# -*- coding: utf-8 -*-
"""PlusVCC 本地 Web 服务的 Flask 路由。"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from flask import Flask, jsonify, request

from vcc_tool import config_store, totp as totp_mod
from vcc_tool.page import INDEX_HTML
from vcc_tool.shortlink import (
    CURRENCY_BY_COUNTRY,
    DEFAULT_PROMO,
    DEFAULT_UA,
    build_sentinel_token,
    create_short_link,
    parse_session,
    query_coupon,
    resolve_proxy,
    sentinel_available,
)
from vcc_tool.vcc_client import VccClient, VccError, new_request_no

logger = logging.getLogger("plusvcc")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False

    # ------------------------------------------------------------ 通用
    def _client() -> VccClient:
        cfg = config_store.load()
        if not cfg.get("api_key") or not cfg.get("api_secret"):
            raise VccError("请先在「设置」里填写并保存 API Key / API Secret")
        return VccClient(
            cfg["api_key"],
            cfg["api_secret"],
            cfg.get("webhook_secret") or "",
            base_url=cfg.get("base_url") or "https://card.ailinkvcc.com",
            timeout=int(cfg.get("timeout") or 40),
            proxy=cfg.get("api_proxy") or "",
        )

    def _fail(exc: Exception, code: str = "vcc_failed", status: int = 200):
        if isinstance(exc, VccError):
            logger.warning("VCC 调用失败: %s (status=%s code=%s)", exc, exc.status, exc.code)
        else:
            logger.exception("VCC 调用异常")
        return jsonify({"ok": False, "code": code, "error": str(exc)}), status

    def _body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 页面
    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/api/quit")
    def quit_app():
        threading.Timer(0.35, lambda: os._exit(0)).start()
        return jsonify({"ok": True})

    # ------------------------------------------------------------ 配置
    @app.get("/api/config")
    @app.post("/api/config")
    def config_route():
        if request.method == "GET":
            return jsonify({"ok": True, "data": config_store.public_view(config_store.load())})
        data = _body()
        allowed = {"base_url", "api_key", "api_secret", "webhook_secret", "timeout", "api_proxy", "chain_upstream"}
        payload = {k: v for k, v in data.items() if k in allowed and v not in (None, "")}
        if not payload:
            return jsonify({"ok": True, "data": config_store.public_view(config_store.load())})
        saved = config_store.save(payload)
        return jsonify({"ok": True, "data": config_store.public_view(saved)})

    # ------------------------------------------------------------ 短链
    @app.post("/api/sentinel/selftest")
    def sentinel_selftest():
        """不碰 session，只验证 sentinel（core + node + sdk.js）能不能出 token。"""
        ok, why = sentinel_available()
        if not ok:
            return jsonify({"ok": False, "code": "sentinel_unavailable", "error": why})
        data = _body()
        proxy_text = str(data.get("proxy") or "").strip()
        device_id = str(data.get("device_id") or "").strip() or str(uuid.uuid4())
        proxy = ""
        note = ""
        handle = None
        started = time.time()
        try:
            if proxy_text:
                try:
                    handle = resolve_proxy(proxy_text, "chrome", 40)
                except Exception as exc:
                    return jsonify({"ok": False, "code": "proxy_failed",
                                    "error": f"代理不可用: {exc}"})
                proxy = handle.url
                note = handle.note or ""
            token, so = build_sentinel_token("", "", device_id, proxy, "chrome", DEFAULT_UA, 45)
        except Exception as exc:
            return jsonify({"ok": False, "code": "sentinel_failed",
                            "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if handle is not None:
                handle.close()
        return jsonify({
            "ok": True,
            "flow": "chatgpt_checkout",
            "elapsed": round(time.time() - started, 1),
            "token_len": len(token),
            "has_so": bool(so),
            "proxy": proxy,
            "proxy_note": note,
        })

    @app.post("/api/shortlink")
    def shortlink():
        data = _body()
        session_text = str(data.get("session") or "").strip()
        proxy_text = str(data.get("proxy") or "").strip()
        if not session_text:
            return jsonify({"ok": False, "code": "bad_request", "error": "请填写 session"}), 400
        if not proxy_text:
            return jsonify({"ok": False, "code": "bad_request", "error": "请填写代理"}), 400

        cfg = config_store.load()
        handle = None
        try:
            token, account_id = parse_session(session_text)
            proxy_type = str(data.get("proxy_type") or "auto").strip().lower()
            chain_upstream = str(data.get("chain_upstream") or "").strip() or cfg.get("chain_upstream") or None
            chain_only_value = data.get("chain_only")
            chain_only = True if chain_only_value is None else bool(chain_only_value)
            try:
                handle = resolve_proxy(proxy_text, "chrome", 60, proxy_type, chain_upstream, chain_only)
            except (RuntimeError, ValueError) as exc:
                return jsonify({"ok": False, "code": "proxy_failed", "error": f"代理连接失败: {exc}"}), 502

            geo = handle.geo
            country = (str(data.get("country") or "").strip() or geo.get("country") or "US").upper()
            currency = (
                str(data.get("currency") or "").strip()
                or geo.get("currency")
                or CURRENCY_BY_COUNTRY.get(country)
                or "USD"
            ).upper()
            device_id = str(uuid.uuid4())
            promo = DEFAULT_PROMO

            coupon = query_coupon(
                token, account_id, device_id, promo, handle.url,
                "chrome", DEFAULT_UA, "Codex Browser", "codex_webview", 60,
                0 if handle.chained else 1,
            )
            if not coupon.get("ok"):
                status = coupon.get("status")
                detail = coupon.get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("detail") or detail.get("error") or detail.get("message") or detail
                if status == 0:
                    reason = "链式请求 chatgpt.com 失败" if handle.chained else "直连代理请求 chatgpt.com 被重置"
                    return jsonify({"ok": False, "code": "proxy_failed",
                                    "error": f"代理连接失败（{reason}）: {detail}"}), 502
                if status == 401:
                    return jsonify({"ok": False, "code": "session_expired",
                                    "error": "session 已失效，请重新获取 /api/auth/session"}), 401
                return jsonify({"ok": False, "code": "check_failed",
                                "error": f"优惠查询失败（HTTP {status}）: {detail}"}), 502
            if not coupon.get("eligible"):
                return jsonify({"ok": False, "code": "no_promo", "message": "无优惠",
                                "state": coupon.get("state") or "ineligible"}), 200

            result = create_short_link(
                token=token,
                account_id=account_id,
                device_id=device_id,
                proxy=handle.url,
                country=country,
                currency=currency,
                plan_name="chatgptplusplan",
                promo=promo,
                impersonate="chrome",
                ua=DEFAULT_UA,
                originator="Codex Browser",
                frontend="codex_webview",
                sentinel_mode="auto",
                timeout=60,
            )
            return jsonify({
                "ok": True,
                "short_url": result["short_url"],
                "checkout_session_id": result["checkout_session_id"],
                "processor_entity": result["processor_entity"],
                "country": country,
                "currency": currency,
                "billing_details": result.get("billing_details"),
                "proxy_ip": geo.get("ip"),
                "geo_source": geo.get("source"),
                "chained": bool(handle.chained),
                "chain_note": handle.note or "",
                "promo": promo,
                "sentinel_used": result.get("sentinel_used"),
                "sentinel_so_used": result.get("sentinel_so_used"),
                "sentinel_error": result.get("sentinel_error") or None,
            })
        except ValueError as exc:
            return jsonify({"ok": False, "code": "bad_request", "error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"ok": False, "code": "generate_failed", "error": str(exc)}), 400
        except Exception as exc:
            logger.exception("短链生成失败")
            return jsonify({"ok": False, "code": "server_error",
                            "error": f"{type(exc).__name__}: {exc}"}), 500
        finally:
            if handle is not None:
                handle.close()

    # ------------------------------------------------------------ VCC
    @app.post("/api/vcc/bins")
    def vcc_bins():
        try:
            return jsonify({"ok": True, "data": _client().card_bins()})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/account-balance")
    def vcc_balance():
        try:
            client = _client()
            data = client.account_balance()
            return jsonify({
                "ok": True,
                "data": data,
                "auto_proxy": bool(client.auto_proxy),
                "proxy": client.proxy,
                "proxy_note": client.proxy_note,
            })
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/account-transactions")
    def vcc_account_transactions():
        data = _body()
        try:
            result = _client().account_transactions(
                page_num=int(data.get("pageNum") or 1),
                page_size=int(data.get("pageSize") or 20),
                bizType=str(data.get("bizType") or ""),
                beginTime=str(data.get("beginTime") or ""),
                endTime=str(data.get("endTime") or ""),
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/apply")
    def vcc_apply():
        data = _body()
        try:
            amount = str(data.get("rechargeAmount") or "").strip()
            if not amount:
                raise VccError("请填写首充金额 rechargeAmount")
            bin_id = data.get("cardBinId")
            if bin_id in (None, ""):
                raise VccError("请选择卡 BIN")
            request_no = str(data.get("requestNo") or "").strip() or new_request_no()
            result = _client().apply_card(
                request_no=request_no,
                card_bin_id=bin_id,
                recharge_amount=amount,
                card_count=int(data.get("cardCount") or 1),
                holder_id=data.get("holderId"),
                remark=str(data.get("remark") or "").strip()[:40],
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/apply-status")
    def vcc_apply_status():
        data = _body()
        try:
            result = _client().apply_status(
                task_id=data.get("taskId"),
                batch_no=str(data.get("batchNo") or ""),
                request_no=str(data.get("requestNo") or ""),
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/cards")
    def vcc_cards():
        data = _body()
        try:
            result = _client().query_cards(
                page_num=int(data.get("pageNum") or 1),
                page_size=int(data.get("pageSize") or 20),
                batchNo=str(data.get("batchNo") or ""),
                remark=str(data.get("remark") or ""),
                cardStatus=str(data.get("cardStatus") or ""),
                cardId=str(data.get("cardId") or ""),
                last4=str(data.get("last4") or ""),
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/reveal")
    def vcc_reveal():
        data = _body()
        card_id = str(data.get("cardId") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "code": "bad_request", "error": "缺少 cardId"}), 400
        try:
            info = _client().private_info(card_id)
            return jsonify({
                "ok": True,
                "data": {
                    "cardId": info["cardId"],
                    "cardNumber": info["cardNumber"],
                    "expiryDate": info["expiryDate"],
                    "cvv": info["cvv"],
                    "alg": info["alg"],
                },
            })
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/balances")
    def vcc_balances():
        data = _body()
        card_ids = data.get("cardIds") or []
        if not isinstance(card_ids, list) or not card_ids:
            return jsonify({"ok": False, "code": "bad_request", "error": "缺少 cardIds"}), 400
        try:
            return jsonify({"ok": True, "data": _client().card_balances([str(c) for c in card_ids])})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/transactions")
    def vcc_transactions():
        data = _body()
        try:
            result = _client().card_transactions(
                page_num=int(data.get("pageNum") or 1),
                page_size=int(data.get("pageSize") or 20),
                keyword=str(data.get("keyword") or ""),
                cardNo=str(data.get("cardNo") or ""),
                merchantName=str(data.get("merchantName") or ""),
                transactionType=str(data.get("transactionType") or ""),
                transactionStatus=str(data.get("transactionStatus") or ""),
                settlementStatus=str(data.get("settlementStatus") or ""),
                beginTime=str(data.get("beginTime") or ""),
                endTime=str(data.get("endTime") or ""),
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/otp")
    def vcc_otp():
        data = _body()
        card_id = str(data.get("cardId") or "").strip()
        card_no = str(data.get("cardNo") or "").strip()
        if not card_id and not card_no:
            return jsonify({"ok": False, "code": "bad_request", "error": "缺少 cardId 或 cardNo"}), 400
        try:
            return jsonify({"ok": True, "data": _client().verification_code(card_id=card_id, card_no=card_no)})
        except Exception as exc:
            return _fail(exc)

    @app.post("/api/vcc/holders")
    def vcc_holders():
        data = _body()
        try:
            result = _client().cardholders(
                page_num=int(data.get("pageNum") or 1),
                page_size=int(data.get("pageSize") or 20),
            )
            return jsonify({"ok": True, "data": result})
        except Exception as exc:
            return _fail(exc)

    # ------------------------------------------------------------ 2FA（TOTP）
    @app.post("/api/totp/codes")
    def totp_codes():
        accounts = config_store.load().get("totp_accounts") or []
        return jsonify({"ok": True, "data": [totp_mod.account_code(item) for item in accounts]})

    @app.post("/api/totp/preview")
    def totp_preview():
        data = _body()
        try:
            parsed = totp_mod.parse_input(str(data.get("secret") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "code": "bad_secret", "error": str(exc)}), 400
        return jsonify({"ok": True, "data": totp_mod.account_code(parsed)})

    @app.post("/api/totp/add")
    def totp_add():
        data = _body()
        try:
            parsed = totp_mod.parse_input(str(data.get("secret") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "code": "bad_secret", "error": str(exc)}), 400
        preset = str(data.get("label") or "").strip()
        label = (preset or parsed["label"] or "ChatGPT").strip()[:60]
        current = config_store.load()
        accounts = list(current.get("totp_accounts") or [])
        for item in accounts:
            if item.get("secret") == parsed["secret"]:
                return jsonify({"ok": False, "code": "duplicate",
                                "error": f"这个密钥已经添加过（{item.get('label') or 'ChatGPT'}）"}), 400
        accounts.append({**parsed, "id": totp_mod.new_account_id(), "label": label})
        config_store.save({"totp_accounts": accounts})
        return jsonify({"ok": True, "data": {"id": accounts[-1]["id"], "label": label}})

    @app.post("/api/totp/remove")
    def totp_remove():
        data = _body()
        account_id = str(data.get("id") or "").strip()
        if not account_id:
            return jsonify({"ok": False, "code": "bad_request", "error": "缺少 id"}), 400
        current = config_store.load()
        accounts = [item for item in (current.get("totp_accounts") or []) if item.get("id") != account_id]
        config_store.save({"totp_accounts": accounts})
        return jsonify({"ok": True, "data": {"removed": len(current.get("totp_accounts") or []) - len(accounts)}})

    return app
