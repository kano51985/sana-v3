from datetime import datetime
import chromadb, time
from sana.config import CHROMA_DB_PATH, COLLECTION_NAME

class MemoryManager:
    def __init__(self):
        self.client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
        self.collection = self.client.get_or_create_collection(name=COLLECTION_NAME)

    def save_consolidated_events(self, events_list, batch_id):
        if not events_list:
            return
        ct = time.time()
        dt = datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S")
        docs, metas, ids = [], [], []
        for i, e in enumerate(events_list):
            if isinstance(e, str):
                docs.append(e)
                metas.append({"batch_id": str(batch_id), "datetime": dt,
                             "entities": "",
                             "memory_type": "consolidated_episodic"})
            else:
                docs.append(e.get("summary", ""))
                metas.append({"batch_id": str(batch_id), "datetime": dt,
                             "entities": ",".join(e.get("entities", [])),
                             "memory_type": "consolidated_episodic"})
            ids.append(f"evt_{batch_id}_{i}")
        self.collection.add(documents=docs, metadatas=metas, ids=ids)

    def recall(self, query_text, n_results=3):
        if not query_text.strip():
            return "No relevant memory."
        results = self.collection.query(query_texts=[query_text], n_results=n_results)
        if not results['documents'] or not results['documents'][0]:
            return "No relevant memory."
        texts = []
        for i in range(len(results['documents'][0])):
            bid = results['metadatas'][0][i].get('batch_id', 'unknown')
            texts.append(f'[ID: {bid}] {results["documents"][0][i]}')
        return '\n'.join(texts)
