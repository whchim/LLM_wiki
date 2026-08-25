"""Streamlit → FastAPI 的 HTTP 客户端（JWT 认证）。

零额外依赖（urllib 标准库）；token 由调用方持有（st.session_state["auth"]）。
异常统一抛 ApiError，UI 层捕获后展示。
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")


class ApiError(Exception):
    """API 调用失败：status 为 HTTP 状态码，message 为可展示文案。"""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ApiClient:
    """带 Bearer token 的 API 客户端。token=None 时仅可调公开端点（login）。"""

    def __init__(self, token: str | None = None):
        self.token = token

    # ---- 底层 ----
    def _request(self, method: str, path: str, *, json_body=None,
                 params: dict | None = None, files: list[tuple] | None = None,
                 form: dict | None = None) -> dict:
        url = API_BASE.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {}
        data = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif files is not None:
            # multipart/form-data（仅上传用；用简单手工边界）
            boundary = "----dshboundary" + os.urandom(8).hex()
            parts = []
            for field, (filename, content, ctype) in files:
                parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n".encode("utf-8") + content + b"\r\n")
            if form:
                for k, v in form.items():
                    parts.append(
                        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                        f"{v}\r\n".encode("utf-8"))
            parts.append(f"--{boundary}--\r\n".encode("utf-8"))
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif form:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("detail", raw)
            except json.JSONDecodeError:
                detail = raw
            raise ApiError(e.code, str(detail)) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"无法连接 API 服务（{e.reason}）。请确认 api 服务已启动。") from None

    # ---- 认证 ----
    @staticmethod
    def login(username: str, password: str) -> dict:
        return ApiClient()._request("POST", "/auth/login",
                                    json_body={"username": username, "password": password})

    # ---- 端口封装（页面调用） ----
    def upload(self, files: list[tuple], category: str) -> dict:
        """files: [(field, (filename, bytes, ctype))]"""
        return self._request("POST", "/uploads", files=files, form={"category": category})

    def list_tasks(self, limit: int = 50) -> list:
        return self._request("GET", "/uploads/tasks", params={"limit": limit})

    def retry_task(self, task_id: int) -> dict:
        return self._request("POST", f"/uploads/tasks/{task_id}/retry")

    def list_pending(self) -> list:
        return self._request("GET", "/reviews/pending")

    def list_rejected(self) -> list:
        return self._request("GET", "/reviews/rejected")

    def approve(self, review_id: int) -> dict:
        return self._request("POST", f"/reviews/{review_id}/approve")

    def reject(self, review_id: int, reason: str) -> dict:
        return self._request("POST", f"/reviews/{review_id}/reject",
                             json_body={"reason": reason})

    def resubmit(self, review_id: int) -> dict:
        return self._request("POST", f"/reviews/{review_id}/resubmit")

    def retry_ai(self, review_id: int) -> dict:
        return self._request("POST", f"/reviews/{review_id}/retry-ai")

    def search(self, query: str) -> dict:
        return self._request("GET", "/search", params={"query": query})

    def missed(self, limit: int = 20) -> dict:
        return self._request("GET", "/search/missed", params={"limit": limit})

    def stats(self) -> dict:
        return self._request("GET", "/search/stats")

    def entries(self) -> dict:
        return self._request("GET", "/entries")

    def rebuild_index(self) -> dict:
        return self._request("POST", "/admin/rebuild-index")