"""SP4 混合检索：DashScope text-embedding-v4 客户端（OpenAI 兼容接口）。

零第三方依赖（urllib）；批量上限 10 条/请求；429/5xx 指数退避重试。
DASHSCOPE_API_KEY 未配置时 is_available()=False —— /search 自动降级 grep-only。

向量 = 可重建缓存（沿"数据库是缓存"铁律）：模型换版或索引损坏时，
/admin/backfill-embeddings 全量重算即可，无权威数据风险。
"""
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
MODEL = "text-embedding-v4"
DIMENSIONS = 1024
BATCH_LIMIT = 10          # DashScope 单请求上限
MAX_RETRY = 2


class EmbeddingError(Exception):
    """embedding 调用失败（网络/限流/鉴权）。调用方应降级为 grep-only。"""


def is_available() -> bool:
    """DASHSCOPE_API_KEY 已配置即视为可用（真实可达性由调用侧降级兜底）。"""
    return bool(os.environ.get("DASHSCOPE_API_KEY"))


def _request(texts: list[str]) -> list[list[float]]:
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    body = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last_err: Exception | None = None
    for attempt in range(MAX_RETRY + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # OpenAI 兼容格式：data[] 按 index 排序返回
            items = sorted(data["data"], key=lambda x: x["index"])
            vecs = [item["embedding"] for item in items]
            if len(vecs) != len(texts):
                raise EmbeddingError(f"返回向量数 {len(vecs)} != 输入 {len(texts)}")
            return vecs
        except urllib.error.HTTPError as e:
            # 429 限流 / 5xx 服务端错误 → 退避重试；4xx 其他（如 401）不重试
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRY:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            raise EmbeddingError(f"DashScope HTTP {e.code}: {e.reason}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt < MAX_RETRY:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            raise EmbeddingError(f"embedding 请求失败: {e}") from (last_err or e)
    raise EmbeddingError(f"embedding 重试耗尽: {last_err}")


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化（自动按 10 条分批）。输入空列表返回空。"""
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH_LIMIT):
        out.extend(_request(texts[i:i + BATCH_LIMIT]))
    return out


def embed_query(text: str) -> list[float]:
    """单条查询向量化。"""
    return embed_texts([text])[0]
