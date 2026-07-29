from pymongo import MongoClient
import uuid, time

class RawMemoryDB:
    def __init__(self, uri='mongodb://localhost:27017/'):
        self.client = MongoClient(uri)
        self.db = self.client['sana_brain']
        self.collection = self.db['raw_dialogue_batches']

    def save_raw_buffer(self, chat_buffer):
        bid = f"batch_{uuid.uuid4().hex[:16]}"
        self.collection.insert_one({"_id": bid, "timestamp": time.time(), "dialogue_log": chat_buffer})
        return bid

    def fetch_raw_memory(self, batch_id):
        doc = self.collection.find_one({"_id": batch_id})
        if not doc:
            return '(No detailed memory found.)'
        logs = doc.get('dialogue_log', [])
        return '[Raw dialogue]:\n' + '\n'.join([f'[{m["role"]}]: {m["content"]}' for m in logs])
