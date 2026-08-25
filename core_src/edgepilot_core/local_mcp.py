"""Small dependency-free stdio MCP and loopback dashboard host.

This module deliberately uses only the Python standard library.  It is imported
by the lightweight plugin layer before a product runtime exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from threading import Thread
import time
from typing import Any, Callable
import urllib.request


MCP_PROTOCOL_VERSION = "2025-06-18"
CARD_MIME = "text/html;profile=mcp-app"
RECOMMEND_SCHEMA = {
    "type": "object",
    "properties": {
        "questionnaire_version": {"type": "string", "const": "2.0"},
        "profit_style": {"type": "string", "enum": ["trend", "reversal", "relative_value"]},
        "holding_period": {"type": "string", "enum": ["intraday", "multi_day", "multi_week"]},
        "pain_point": {"type": "string", "enum": ["loss_streak", "inactivity", "tail_loss"]},
        "max_drawdown_pct": {"type": "number", "enum": [5, 15, 20]},
        "trading_mode": {"type": "string", "enum": ["long_only_no_leverage", "long_short_low_leverage", "long_short_high_leverage"]},
        "allocation_band": {"type": "string", "enum": ["under_25k", "25k_100k", "over_100k"]},
        "universe": {"type": "string", "enum": ["majors", "altcoins", "any"]},
        "locale": {"type": "string", "enum": ["en", "ko", "zh-CN", "zh-TW"]},
    },
    "required": ["questionnaire_version", "profit_style", "holding_period", "pain_point", "max_drawdown_pct",
                 "trading_mode", "allocation_band", "universe", "locale"],
    "additionalProperties": False,
}


def _app_meta(ui_uri: str) -> dict[str, Any]:
    """Bind a tool to its MCP App while retaining ChatGPT compatibility aliases."""
    return {
        "ui": {"resourceUri": ui_uri},
        "ui/resourceUri": ui_uri,
        "openai/outputTemplate": ui_uri,
    }


@dataclass(frozen=True)
class ProductConfig:
    name: str
    server_name: str
    version: str
    state_root: Path
    default_port: int
    ui_uri: str
    lifecycle_prompt: str
    service_id: str
    windows_task: str
    identity_attribute: str = "edgepilot_identity"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _pid_exists(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _trusted_dashboard(record: object, config: ProductConfig) -> dict[str, Any] | None:
    if not isinstance(record, dict) or set(record) != {"pid", "version", "instance_nonce", "host", "port", "url"}:
        return None
    if record.get("version") != config.version or record.get("host") != "127.0.0.1" or not _pid_exists(record.get("pid")):
        return None
    port, nonce = record.get("port"), record.get("instance_nonce")
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(nonce, str) or len(nonce) < 20:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Host": f"127.0.0.1:{port}", "X-EdgePilot-Instance": nonce},
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            value = json.loads(response.read(4097))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return record if value == record else None


def _card_html(product: str) -> str:
    product_json = json.dumps(product, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<style>body{font:14px system-ui;margin:0;color:#182033}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}article{border:1px solid #dfe3eb;border-radius:12px;padding:16px;background:#fff}h2{font-size:16px;margin:8px 0}p{color:#596174;line-height:1.45}small{color:#747d91}@media(max-width:720px){.grid{grid-template-columns:1fr}}</style></head>
<body><div id="root"></div><script>
const root=document.getElementById('root');
const product=__PRODUCT_JSON__;
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
let hasAuthoritativeResult=false;
function renderStatus(message){root.innerHTML=`<p>${escapeHtml(product)} ${message}</p>`;}
function renderLoading(){renderStatus('is loading strategy recommendations.');}
function renderError(){renderStatus('could not load strategy cards.');}
function supportedRows(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return null;
  for(const key of ['recommendations','cards','strategies'])if(Array.isArray(value[key]))return value[key];
  return null;
}
function renderRows(rows){
  if(rows.some(row=>!row||typeof row!=='object'||Array.isArray(row))){renderError();return;}
  const cards=rows.slice(0,3);
  root.innerHTML=cards.length?`<div class="grid">${cards.map((row,i)=>`<article><small>${escapeHtml(row.role||row.category||`#${i+1}`)}</small><h2>${escapeHtml(row.display_name||row.name||row.title||row.slug||'Strategy')}</h2><p>${escapeHtml(row.summary||row.reason||row.description||'')}</p></article>`).join('')}</div>`:`<p>${escapeHtml(product)} returned no strategy cards.</p>`;
}
function acceptCompatible(value){const rows=supportedRows(value);if(rows===null)return false;hasAuthoritativeResult=true;renderRows(rows);return true;}
function acceptStandard(params){
  hasAuthoritativeResult=true;
  if(params?.isError===true){renderError();return;}
  const rows=supportedRows(params?.structuredContent);
  if(rows===null){renderError();return;}
  renderRows(rows);
}
renderLoading();
let nextId=1;const pending=new Map();
window.addEventListener('message',event=>{if(event.source!==window.parent)return;const message=event.data;if(!message||message.jsonrpc!=='2.0')return;
if(message.id!==undefined&&pending.has(message.id)){const callback=pending.get(message.id);pending.delete(message.id);message.error?callback.reject(message.error):callback.resolve(message.result);return;}
if(message.method==='ui/notifications/tool-result')acceptStandard(message.params);});
window.addEventListener('openai:set_globals',event=>acceptCompatible(event.detail?.globals?.toolOutput));
acceptCompatible(window.openai?.toolOutput);
function request(method,params){const id=nextId++;window.parent.postMessage({jsonrpc:'2.0',id,method,params},'*');return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}));}
request('ui/initialize',{appInfo:{name:'edgepilot-strategy-cards',version:'2.0.0'},appCapabilities:{},protocolVersion:'2026-01-26'})
.then(()=>{window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized'},'*');acceptCompatible(window.openai?.toolOutput);})
.catch(()=>{if(!hasAuthoritativeResult)renderError();});
</script></body></html>""".replace("__PRODUCT_JSON__", product_json)


def _dashboard_html(config: ProductConfig, url: str) -> bytes:
    return f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>{html.escape(config.name)}</title>
<style>body{{font:15px system-ui;max-width:760px;margin:64px auto;padding:0 24px;color:#182033}}section{{border:1px solid #dfe3eb;border-radius:14px;padding:24px}}code{{background:#f4f5f8;padding:3px 6px;border-radius:5px}}.ok{{color:#16803a}}</style></head><body><section><p class=\"ok\">● 本地服务正在运行</p><h1>{html.escape(config.name)}</h1><p>{html.escape(config.lifecycle_prompt)}</p><p>当前地址：<code>{html.escape(url)}</code></p><p>可在 ChatGPT 对话中要求推荐策略，结果会显示为最多三张卡片。</p></section></body></html>""".encode()


class _Handler(BaseHTTPRequestHandler):
    server_version = "EdgePilotLocal/1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"dashboard {format % args}", file=sys.stderr, flush=True)

    def do_GET(self) -> None:  # noqa: N802
        identity = getattr(self.server, "edgepilot_identity")
        expected_host = f"127.0.0.1:{identity['port']}"
        if self.headers.get("Host") != expected_host:
            self.send_error(403)
            return
        if self.path == "/api/health":
            if not secrets.compare_digest(self.headers.get("X-EdgePilot-Instance", ""), identity["instance_nonce"]):
                self.send_error(403)
                return
            body = json.dumps(identity, separators=(",", ":")).encode()
            content_type = "application/json"
        elif self.path in {"/", "/index.html"}:
            body = getattr(self.server, "edgepilot_html")
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Dashboard:
    def __init__(self, config: ProductConfig, server_factory: Callable[[str, int], ThreadingHTTPServer] | None = None,
                 *, reuse_existing: bool = True) -> None:
        self.config = config
        self.record_path = config.state_root / "local-dashboard.json"
        self.server: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        self.identity: dict[str, Any] | None = None
        self.error: str | None = None
        self.server_factory = server_factory
        self.reuse_existing = reuse_existing

    def start(self) -> None:
        try:
            old = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            old = None
        trusted = _trusted_dashboard(old, self.config) if self.reuse_existing else None
        if trusted is not None:
            self.identity = trusted
            return
        last_error: OSError | None = None
        for port in (self.config.default_port, 0):
            try:
                self.server = (self.server_factory("127.0.0.1", port) if self.server_factory
                               else ThreadingHTTPServer(("127.0.0.1", port), _Handler))
                break
            except OSError as error:
                last_error = error
        if self.server is None:
            self.error = f"本地网站启动失败：{last_error}. 请关闭占用端口的程序后重试。"
            return
        actual_port = int(self.server.server_address[1])
        nonce = secrets.token_urlsafe(24)
        url = f"http://127.0.0.1:{actual_port}"
        self.identity = {"pid": os.getpid(), "version": self.config.version, "instance_nonce": nonce,
                         "host": "127.0.0.1", "port": actual_port, "url": url}
        self.server.edgepilot_identity = self.identity  # type: ignore[attr-defined]
        setattr(self.server, self.config.identity_attribute, self.identity)
        self.server.edgepilot_html = _dashboard_html(self.config, url)  # type: ignore[attr-defined]
        _atomic_json(self.record_path, self.identity)
        self.thread = Thread(target=self.server.serve_forever, name=f"{self.config.server_name}-dashboard", daemon=True)
        self.thread.start()

    def ensure(self) -> None:
        """Keep a reused Dashboard usable if its original MCP owner exits."""
        if self.server is not None:
            return
        if self.identity is not None and _trusted_dashboard(self.identity, self.config) is not None:
            return
        self.identity = None
        self.error = None
        self.start()

    def close(self) -> None:
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        try:
            current = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            current = None
        if current == self.identity:
            self.record_path.unlink(missing_ok=True)


def _cards(value: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("recommendations", "cards", "strategies", "items"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)][:3]
    return []


def run(config: ProductConfig, recommend: Callable[[dict[str, Any]], dict[str, Any]],
        server_factory: Callable[[str, int], ThreadingHTTPServer] | None = None) -> int:
    dashboard = Dashboard(config, server_factory)
    dashboard.start()
    ui_html = _card_html(config.name)

    def respond(request_id: object, result: object = None, error: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id}
        payload["error" if error else "result"] = error or result
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)

    try:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("message must be an object")
            except (ValueError, json.JSONDecodeError) as exc:
                respond(None, error={"code": -32700, "message": str(exc)})
                continue
            method, request_id = message.get("method"), message.get("id")
            if request_id is None:
                continue
            if method == "initialize":
                respond(request_id, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}, "resources": {}},
                                     "serverInfo": {"name": config.server_name, "version": config.version}})
            elif method == "ping":
                respond(request_id, {})
            elif method == "resources/list":
                respond(request_id, {"resources": [{"uri": config.ui_uri, "name": f"{config.name} strategy cards", "mimeType": CARD_MIME,
                                                    "_meta": {"ui": {"prefersBorder": False}}}]})
            elif method == "resources/read":
                uri = (message.get("params") or {}).get("uri")
                if uri != config.ui_uri:
                    respond(request_id, error={"code": -32602, "message": "unknown resource"})
                else:
                    respond(request_id, {"contents": [{"uri": config.ui_uri, "mimeType": CARD_MIME, "text": ui_html,
                                                       "_meta": {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}}}}]})
            elif method == "tools/list":
                respond(request_id, {"tools": [
                    {"name": "recommend_strategies", "title": f"Recommend {config.name} strategies",
                     "description": "Return at most three server-ranked strategy recommendations.",
                     "inputSchema": RECOMMEND_SCHEMA,
                     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
                     "_meta": _app_meta(config.ui_uri)},
                    {"name": "open_dashboard", "title": f"Open {config.name} local website",
                     "description": "Return the real loopback URL for the current local website.",
                     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
                    {"name": "enable_persistent_dashboard", "title": f"Keep {config.name} running",
                     "description": f"Install the fixed local background service {config.service_id}. Requires explicit confirmation.",
                     "inputSchema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"], "additionalProperties": False},
                     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
                    {"name": "disable_persistent_dashboard", "title": f"Restore {config.name} to Codex lifetime",
                     "description": f"Remove only the fixed local background service {config.service_id}. Requires explicit confirmation.",
                     "inputSchema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}, "required": ["confirm"], "additionalProperties": False},
                     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
                ]})
            elif method == "tools/call":
                params = message.get("params") or {}
                name, arguments = params.get("name"), params.get("arguments") or {}
                try:
                    if name == "recommend_strategies":
                        value = recommend(arguments)
                        rows = _cards(value)
                        structured = {**value, "recommendations": rows}
                        summary = f"{config.name} 返回了 {len(rows)} 个策略推荐。"
                        respond(request_id, {"content": [{"type": "text", "text": summary}], "structuredContent": structured})
                    elif name == "open_dashboard":
                        dashboard.ensure()
                        if dashboard.identity is None:
                            raise RuntimeError(dashboard.error or "本地网站不可用，请重试。")
                        url = dashboard.identity["url"]
                        respond(request_id, {"content": [{"type": "text", "text": f"{url}\n\n{config.lifecycle_prompt}"}],
                                             "structuredContent": {"url": url, "lifecycle": config.lifecycle_prompt}})
                    elif name in {"enable_persistent_dashboard", "disable_persistent_dashboard"}:
                        if arguments != {"confirm": True}:
                            raise RuntimeError(f"此操作会修改本机后台启动项 {config.service_id}；确认后再执行。")
                        from edgepilot_core import persistent_service
                        action = persistent_service.install if name.startswith("enable") else persistent_service.uninstall
                        value = action(config, service_id=config.service_id, windows_task=config.windows_task)
                        respond(request_id, {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value})
                    else:
                        respond(request_id, error={"code": -32602, "message": "unknown tool"})
                except Exception as exc:
                    respond(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            else:
                respond(request_id, error={"code": -32601, "message": "method not found"})
    finally:
        dashboard.close()
    return 0
