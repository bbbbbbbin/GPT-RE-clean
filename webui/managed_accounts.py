from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, render_template, request

from core import managed_accounts_service as svc

bp = Blueprint("managed_accounts", __name__)


def _error(exc: Exception, status: int = 400):
    return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), status


@bp.get("/managed-accounts")
def page():
    return render_template("managed_accounts.html")


@bp.get("/api/managed-accounts")
def api_list():
    try:
        return jsonify({"ok": True, "accounts": svc.list_accounts()})
    except Exception as exc:
        return _error(exc, 500)


@bp.post("/api/managed-accounts/import")
def api_import():
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "").strip()
        proxies = str(data.get("proxies") or data.get("proxy_text") or "").strip()
        if not text:
            raise ValueError("导入内容为空")
        return jsonify({"ok": True, **svc.import_text(text, proxies)})
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/login")
def api_login(managed_id: int):
    try:
        return jsonify(svc.start_login(managed_id))
    except Exception as exc:
        return _error(exc)



@bp.post("/api/managed-accounts/<int:managed_id>/tag")
def api_tag(managed_id: int):
    try:
        data = request.get_json(silent=True) or {}
        return jsonify(svc.update_tag(managed_id, str(data.get("tag") or "")))
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/rotate-proxy")
def api_rotate_proxy(managed_id: int):
    try:
        return jsonify(svc.rotate_proxy(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/device-login")
def api_device_login(managed_id: int):
    try:
        return jsonify(svc.start_device_login(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.get("/api/managed-accounts/<int:managed_id>/device-login")
def api_device_login_status(managed_id: int):
    try:
        return jsonify(svc.device_login_status(managed_id))
    except Exception as exc:
        return _error(exc)
@bp.post("/api/managed-accounts/<int:managed_id>/device-login/clear")
def api_device_login_clear(managed_id: int):
    try:
        return jsonify(svc.cancel_device_login(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/device-login/restart")
def api_device_login_restart(managed_id: int):
    try:
        return jsonify(svc.restart_device_login(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/device-login/continue")
def api_device_login_continue(managed_id: int):
    try:
        return jsonify(svc.continue_device_login(managed_id))
    except Exception as exc:
        return _error(exc)



@bp.post("/api/managed-accounts/<int:managed_id>/quota")
def api_quota(managed_id: int):
    try:
        return jsonify(svc.check_quota(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.get("/api/managed-accounts/<int:managed_id>/reset-cards")
def api_reset_cards(managed_id: int):
    try:
        return jsonify(svc.reset_cards(managed_id))
    except Exception as exc:
        return _error(exc)


@bp.post("/api/managed-accounts/<int:managed_id>/consume-reset-card")
def api_consume_reset_card(managed_id: int):
    try:
        data = request.get_json(silent=True) or {}
        return jsonify(svc.consume_reset_card(managed_id, str(data.get("credit_id") or "")))
    except Exception as exc:
        return _error(exc)


@bp.get("/api/managed-accounts/<int:managed_id>/rt")
def api_rt(managed_id: int):
    try:
        account = svc.get_account(managed_id)
        if not account:
            raise ValueError("账号不存在")
        content = svc.rt_json(managed_id)
        filename = f"codex-{str(account.get('email') or 'account')}.json"
        return Response(
            content,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
            },
        )
    except Exception as exc:
        return _error(exc, 404)


@bp.delete("/api/managed-accounts/<int:managed_id>")
def api_delete(managed_id: int):
    try:
        return jsonify({"ok": svc.delete_account(managed_id)})
    except Exception as exc:
        return _error(exc)
