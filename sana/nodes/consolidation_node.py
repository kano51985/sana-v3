import json

from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.memory_summarizer import MemorySummarizer
from sana.services.mongo_client import RawMemoryDB
from sana.services.memory_service import MemoryManager
from sana.services.profile_manager import ProfileManager

class ConsolidationNode(PipelineNode):
    AUTO_THRESHOLD = 20

    def __init__(self, summarizer: MemorySummarizer, raw_db: RawMemoryDB, memory: MemoryManager, profile_mgr: ProfileManager):
        self.summarizer = summarizer
        self.raw_db = raw_db
        self.memory = memory
        self.profile_mgr = profile_mgr
        self._pending_batch_id = None
        self._pending_signature = None

    def process(self, ctx: Context) -> NodeResult:
        self.consolidate(ctx.chat_buffer, force=False)
        return NodeResult(context=ctx)

    def consolidate(self, chat_buffer: list, force: bool = False) -> dict:
        if not chat_buffer:
            return self._status("empty", "当前没有待聚合的对话")
        if not force and len(chat_buffer) < self.AUTO_THRESHOLD:
            return self._status("skipped", f"缓存 {len(chat_buffer)} 条，未达到自动聚合阈值")

        batch = list(chat_buffer)
        bid = None
        try:
            profile = self.profile_mgr.load_profile()
            result = self.summarizer.consolidate_buffer(batch, profile)
            if not result or not result.get("ok"):
                error = (result or {}).get("error") or "总结失败"
                return self._status("error", error, batch_id=None, cleared=False, error=error)

            events = result.get("events", []) or []
            updates = result.get("profile_updates", []) or []
            if not isinstance(events, list):
                events = []
            if not isinstance(updates, list):
                updates = []

            try:
                bid = self._get_or_save_raw_batch(batch)
                if events:
                    self.memory.save_consolidated_events(events, batch_id=bid)
                applied_updates = 0
                if updates:
                    applied_updates = int(self.profile_mgr.apply_batch_updates(updates) or 0)
            except Exception as e:
                return self._status(
                    "error", f"写入长期记忆失败: {e}",
                    batch_id=bid, cleared=False, error=str(e)
                )

            chat_buffer.clear()
            self._pending_batch_id = None
            self._pending_signature = None
            return self._status(
                "success", f"已聚合 {len(batch)} 条对话",
                batch_id=bid, event_count=len(events), update_count=applied_updates, cleared=True
            )
        except Exception as e:
            return self._status("error", f"聚合失败: {e}", batch_id=bid, cleared=False, error=str(e))

    def _get_or_save_raw_batch(self, batch):
        signature = json.dumps(batch, ensure_ascii=False, sort_keys=True)
        if signature == self._pending_signature and self._pending_batch_id:
            return self._pending_batch_id
        bid = self.raw_db.save_raw_buffer(batch)
        self._pending_batch_id = bid
        self._pending_signature = signature
        return bid

    def _status(self, code, message, batch_id=None, event_count=0, update_count=0, cleared=False, error=None):
        return {
            "ok": code in ("success", "empty", "skipped"),
            "code": code,
            "message": message,
            "batch_id": batch_id,
            "event_count": event_count,
            "update_count": update_count,
            "cleared": cleared,
            "error": error,
        }
