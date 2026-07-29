import json, os

class ProfileManager:
    def __init__(self, file_path='user_profile.json'):
        self.file_path = file_path
        if not os.path.exists(self.file_path):
            self.save_profile({"name": "白日", "gaming_preferences": {}, "general_preferences": {}})

    def load_profile(self):
        with open(self.file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_profile(self, data):
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def apply_batch_updates(self, updates):
        if not updates: return
        prof = self.load_profile()
        changed = False
        for u in updates:
            act = u.get('action', '')
            cat = u.get('category', 'general_preferences')
            key = u.get('key', '')
            val = u.get('value', '')
            if cat not in prof or not isinstance(prof[cat], dict):
                prof[cat] = {}
            if act in ['update', 'add']:
                prof[cat][key] = val; changed = True
            elif act == 'delete':
                if key in prof[cat]:
                    del prof[cat][key]; changed = True
        if changed:
            self.save_profile(prof)
