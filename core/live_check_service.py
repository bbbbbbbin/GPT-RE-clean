# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import random
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import _mask_proxy, open_plan_check_proxy, resolve_plan_check_route

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_FUTURES: dict[int, object] = {}
_STOP_GENERATION = 0
_LOCK = threading.Lock()
_NETWORK_RETRY_HINTS = (
    "403", "429", "502", "503", "504", "proxy", "socks", "timeout",
    "timed out", "connection", "closed", "reset",
)


def _configured_live_check_proxies(proxy_cfg) -> list[str]:
    """查活优先使用套餐专用代理池，未配置时使用注册代理池。"""
    values = list(getattr(proxy_cfg, "PLAN_CHECK_PROXY", []) or [])
    if not values:
        getter = getattr(proxy_cfg, "active_proxy_pool", None)
        values = list((getter() if callable(getter) else getattr(proxy_cfg, "PROXY_POOL", [])) or [])
    return [str(value).strip() for value in values if str(value).strip()]


def _live_proxy_route(proxy_url: str, proxy_cfg) -> dict:
    route = resolve_plan_check_route(explicit_proxy=proxy_url)
    upstream = str(getattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "") or "").strip()
    if upstream:
        route["upstream_proxy"] = upstream
        route["upstream_proxy_used"] = _mask_proxy(upstream) or None
        route["network_route"] = "proxy_chain"
    route["proxy_mode"] = "proxy_pool"
    return route


def _retryable_live_check_result(result: dict) -> bool:
    if result.get("ok") or result.get("status") == "deactivated":
        return False
    text = str(result.get("error") or "").lower()
    return any(hint in text for hint in _NETWORK_RETRY_HINTS)


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    with _LOCK:
        if int(acc["id"]) in _RUNNING:
            return True
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _safe_append_log(email: str, line: str, *, clear: bool = False) -> None:
    """辅助日志失败不能覆盖查活结果或阻止保存已经刷新的 AT。"""
    try:
        _append_log(email, line, clear=clear)
    except Exception:
        logger.warning("[查活] 日志写入失败: %s", email, exc_info=True)


def _close_relay(relay) -> None:
    if relay is not None:
        try:
            relay.close()
        except Exception:
            logger.warning("[查活] 代理中继关闭失败", exc_info=True)


def _generation() -> int:
    with _LOCK:
        return _STOP_GENERATION


def _stop_requested(generation: int) -> bool:
    with _LOCK:
        return _STOP_GENERATION != generation


def _forget_future(account_id: int, future) -> None:
    with _LOCK:
        if _FUTURES.get(int(account_id)) is future:
            _FUTURES.pop(int(account_id), None)


def stop_all(reason: str = "已手动停止全部查活") -> dict:
    """停止全部查活：取消未开始的任务，运行中的任务在下一个检查点退出。"""
    global _STOP_GENERATION
    with _LOCK:
        _STOP_GENERATION += 1
        generation = _STOP_GENERATION
        futures = list(_FUTURES.items())
    cancelled = 0
    for account_id, future in futures:
        try:
            if future.cancel():
                cancelled += 1
        except Exception:
            logger.warning("[查活] 取消排队任务失败: account_id=%s", account_id, exc_info=True)
    try:
        marked = db.stop_account_live_checks(reason=reason)
    except Exception:
        logger.exception("[查活] 停止任务写库失败")
        marked = 0
    logger.warning(
        "[查活] 已停止全部任务：取消排队 %s，写库 %s，generation=%s，reason=%s",
        cancelled, marked, generation, reason,
    )
    return {"cancelled": cancelled, "marked": marked, "generation": generation, "reason": reason}


def complete_authenticated_live_check(*, account_id: int, expected_account: dict, result: dict, session, run_id: str | None = None) -> dict:
    """先持久化新 AT，再同步复用登录会话查套餐；套餐失败不改变查活成功。"""
    if not db.update_account_liveness(account_id, result, expected_account=expected_account, expected_run_id=run_id):
        return {"ok": False, "status": "failed", "error": "账号已删除或邮箱/AT 已变更，丢弃旧查活结果"}
    try:
        from core.plan_check_service import check_account_plan_in_session
        result["plan_result"] = check_account_plan_in_session(
            account_id=account_id, email=expected_account["email"],
            access_token=result["access_token"], session=session,
        )
    except Exception as exc:
        logger.exception("[查活] 自动查套餐异常: account_id=%s", account_id)
        result["plan_result"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    _safe_append_log(expected_account["email"], "[查活] 自动查套餐：" +
                     ("成功" if result["plan_result"].get("ok") else "失败 " + str(result["plan_result"].get("error") or "")))
    return result


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str, run_id: str | None = None) -> dict:
    relay = None
    generation = _generation()
    expected_account = {"email": email}
    completed_in_session = False
    def authenticated(result, session):
        nonlocal completed_in_session
        completed = complete_authenticated_live_check(account_id=account_id, expected_account=expected_account,
                                                       result=result, session=session, run_id=run_id)
        # 只有回调正常返回才算处理完成；保存 AT 抛错时仍需落库最终失败状态。
        completed_in_session = True
        return completed
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id, run_id=run_id):
            _safe_append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        if _stop_requested(generation):
            _safe_append_log(email, "[查活] 已被手动停止，跳过执行")
            return {"ok": False, "status": "failed", "error": "查活已被手动停止"}
        # 查活必须沿用账号注册时记录的邮箱来源。不能只调用
        # resolve_email_source(email)：Remail 等临时邮箱的上下文只在领取进程
        # 内存中存在，服务重启后按当前 EMAIL_SOURCE 推断会把来源判错。
        account = db.get_account(account_id) or {}
        if not account or str(account.get("email") or "").strip().lower() != email.strip().lower():
            result = {"ok": False, "status": "failed", "error": "账号已删除或邮箱已变更，丢弃旧查活任务"}
            db.update_account_liveness(account_id, result, expected_account=expected_account, expected_run_id=run_id)
            return result
        from config import proxy as proxy_cfg
        candidates = _configured_live_check_proxies(proxy_cfg)
        if candidates:
            # 只要配置了查活代理池，就强制使用池内当前代理。账号表里的
            # proxy_used 是注册时的旧出口，代理账号/会话过期后认证必失败，
            # 不能让它抢在代理池前面。
            route = _live_proxy_route(random.choice(candidates), proxy_cfg)
            if str(proxy or "").strip():
                _safe_append_log(email, "[查活] 已配置代理池，忽略调用方传入的代理")
            _safe_append_log(email, "[查活] 使用查活代理池（不复用账号注册时保存的旧代理）")
        else:
            if not str(proxy or "").strip():
                proxy = str(account.get("proxy_used") or "").strip() or None
                if proxy:
                    _safe_append_log(email, "[查活] 未配置查活代理池，使用账号注册时保存的代理")
            route = resolve_plan_check_route(explicit_proxy=proxy)
        selected_proxy = str(route.get("proxy") or "").strip()
        timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
        expected_account = {"email": email, "access_token": account.get("access_token", "")}
        email_source = str(account.get("email_source") or "").strip() or None
        if email_source:
            _safe_append_log(email, f"[查活] 使用注册时保存的邮箱来源：{email_source}")
        _safe_append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        # 每个代理出口使用独立 BrowserSession/指纹/Cookie。网络类失败后换池中
        # 尚未使用的代理；池非空时绝不直连。
        max_routes = max(1, int(getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 3) or 3))
        used_proxies: set[str] = set()
        result: dict = {"ok": False, "status": "failed", "error": "查活未执行"}
        for route_attempt in range(1, max_routes + 1):
            if _stop_requested(generation):
                _safe_append_log(email, "[查活] 已被手动停止，停止后续出口尝试")
                return {"ok": False, "status": "failed", "error": "查活已被手动停止"}
            selected_proxy = str(route.get("proxy") or "").strip()
            if selected_proxy:
                used_proxies.add(selected_proxy)
            effective_proxy, relay = open_plan_check_proxy(
                route, selected_proxy, timeout=timeout,
            )
            _safe_append_log(
                email,
                f"[查活] 网络出口尝试 {route_attempt}/{max_routes}："
                f"{_mask_proxy(selected_proxy) or 'direct'}",
            )
            # 占用属于本次出口尝试，不得沿用上一次失败回调的完成标志。
            completed_in_session = False
            result = check_account_liveness(
                email,
                proxy=effective_proxy,
                clear_log=False,
                email_source=email_source,
                fingerprint_state={"force_fresh": route_attempt > 1},
                on_authenticated=authenticated,
            )
            if relay is not None:
                _close_relay(relay)
                relay = None
            if _stop_requested(generation):
                _safe_append_log(email, "[查活] 已被手动停止，丢弃本次结果")
                return {"ok": False, "status": "failed", "error": "查活已被手动停止"}
            if not _retryable_live_check_result(result):
                break

            remaining = [item for item in candidates if item not in used_proxies]
            if remaining and route_attempt < max_routes:
                next_proxy = random.choice(remaining)
                _safe_append_log(
                    email,
                    "[查活] 当前代理发生网络/403错误，放弃该会话并随机切换下一代理："
                    f"{_mask_proxy(next_proxy)}",
                )
                route = _live_proxy_route(next_proxy, proxy_cfg)
                continue

            if candidates:
                break

            # 没有任何代理池数据时，保留原先的一次直连兜底行为。
            if selected_proxy and route_attempt < max_routes:
                _safe_append_log(
                    email,
                    "[查活] 未配置可替换代理，启动独立直连会话兜底一次",
                )
                route = resolve_plan_check_route(explicit_proxy="")
                continue
            break
        if _stop_requested(generation):
            _safe_append_log(email, "[查活] 已被手动停止，丢弃本次结果")
            return {"ok": False, "status": "failed", "error": "查活已被手动停止"}
        if not completed_in_session and not db.update_account_liveness(account_id, result, expected_account=expected_account, expected_run_id=run_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或邮箱/AT 已变更，丢弃旧查活结果"}
        if result.get("ok"):
            _safe_append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
        elif result.get("status") == "deactivated":
            _safe_append_log(email, f"[查活] 完成：账号已废 {result.get('error') or ''}")
        else:
            _safe_append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        result.update({
            "network_route": route.get("network_route"),
            "proxy_used": _mask_proxy(selected_proxy) or None,
            "upstream_proxy_used": route.get("upstream_proxy_used"),
            "proxy_mode": route.get("proxy_mode"),
            "proxy_fallback_reason": route.get("proxy_fallback_reason"),
        })
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result, expected_account=expected_account, expected_run_id=run_id)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _safe_append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        try:
            if relay is not None:
                _close_relay(relay)
        finally:
            with _LOCK:
                _RUNNING.discard(int(account_id))
            _QUEUE_SLOTS.release()


def _release_cancelled_check(future, account_id: int, run_id: str | None = None) -> None:
    # 被取消且尚未执行的 Future 不会进入 worker 的 finally，必须单独归还占用。
    if not future.cancelled():
        return
    result = {"ok": False, "status": "failed", "error": "任务在开始执行前被取消",
              "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        db.update_account_liveness(account_id, result, expected_run_id=run_id)
    except Exception:
        logger.exception("写入取消状态失败: account_id=%s", account_id)
    finally:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None, return_future: bool = False, executor: ThreadPoolExecutor | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    with _LOCK:
        if account_id in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该账号正在查活"}
        _RUNNING.add(account_id)
    if not _QUEUE_SLOTS.acquire(blocking=False):
        with _LOCK:
            _RUNNING.discard(account_id)
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    run_id = uuid.uuid4().hex
    try:
        claimed = db.claim_account_live_check(acc_id=account_id, trigger=trigger, run_id=run_id, expected_email=email)
    except Exception:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        raise
    if not claimed:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    try:
        _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
        future = (executor or _EXECUTOR).submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
            run_id=run_id,
        )
        with _LOCK:
            _FUTURES[account_id] = future
        future.add_done_callback(lambda done: _release_cancelled_check(done, account_id, run_id))
        future.add_done_callback(lambda done: _forget_future(account_id, done))
    except Exception as exc:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result, expected_run_id=run_id)
        try:
            _append_log(email, result["error"])
        except Exception:
            logger.exception("[查活] 入队失败日志写入异常")
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        **({"future": future} if return_future else {}),
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
