from sana.core.node import PipelineNode, NodeResult
from sana.core.context import Context
from sana.services.memory_summarizer import MemorySummarizer
from sana.services.mongo_client import RawMemoryDB
from sana.services.memory_service import MemoryManager
from sana.services.profile_manager import ProfileManager

class ConsolidationNode(PipelineNode):
    def __init__(self, summarizer: MemorySummarizer, raw_db: RawMemoryDB, memory: MemoryManager, profile_mgr: ProfileManager):
        self.summarizer = summarizer
        self.raw_db = raw_db
        self.memory = memory
        self.profile_mgr = profile_mgr
    def process(self, ctx: Context) -> NodeResult:
        buf = ctx.chat_buffer
        if len(buf) >= 4:
            print(f"[总结层] 缓存已达 {len(buf)} 条，开始聚合...")
            try:
                batch = list(buf)
                bid = self.raw_db.save_raw_buffer(buf)
                buf.clear()
                prof = self.profile_mgr.load_profile()
                result = self.summarizer.consolidate_buffer(batch, prof)
                if result:
                    evts = result.get("events", [])
                    upds = result.get("profile_updates", [])
                    if evts:
                        self.memory.save_consolidated_events(evts, batch_id=bid)
                    if upds:
                        self.profile_mgr.apply_batch_updates(upds)
                    print(f"[总结层] 已保存 {len(evts)} 个事件, {len(upds)} 条档案更新")
            except Exception as e:
                print(f"Consolidation error: {e}")
        return NodeResult(context=ctx)
