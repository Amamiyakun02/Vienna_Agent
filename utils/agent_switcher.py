import os
import json
from glob import glob
from utils import build_instruction

class AgentPersonalityManager:
    """
    Class ini bertugas untuk me-manage berbagai kepribadian (personality) 
    dari file-file JSON yang ada di dalam folder 'agents'.
    """
    
    def __init__(self, agents_dir: str = "agents"):
        self.agents_dir = agents_dir
        self.available_agents = {}
        self._load_all_agents()

    def _load_all_agents(self):
        """Membaca seluruh file .json di folder agents dan menyimpannya di memory"""
        search_path = os.path.join(self.agents_dir, "*.json")
        for file_path in glob(search_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    agent_data = json.load(f)
                    
                    # Menggunakan nama file (tanpa .json) sebagai kunci (key)
                    # Misal file 'kurisu.json' -> key: 'kurisu'
                    agent_key = os.path.basename(file_path).replace(".json", "").lower()
                    
                    # Simpan data personality beserta instruksi yang sudah di-build
                    self.available_agents[agent_key] = {
                        "raw_data": agent_data,
                        "instruction": build_instruction(agent_data),
                        "name": agent_data.get("name", agent_key)
                    }
            except Exception as e:
                print(f"Gagal memuat {file_path}: {e}")

    def get_available_agents(self) -> list:
        """Mengembalikan daftar agent yang tersedia"""
        return [{"id": k, "name": v["name"]} for k, v in self.available_agents.items()]

    def get_agent_instruction(self, agent_id: str) -> str:
        """
        Mengambil instruksi spesifik untuk agent tertentu.
        Jika nama tidak ditemukan, akan fallback ke agent pertama yang diload.
        """
        agent_id = agent_id.lower()
        if agent_id in self.available_agents:
            return self.available_agents[agent_id]["instruction"]
        
        # Fallback jika agent tidak ditemukan
        fallback_key = list(self.available_agents.keys())[0]
        return self.available_agents[fallback_key]["instruction"]

    def reload_agents(self):
        """Berguna jika ada penambahan/perubahan file JSON saat server masih berjalan"""
        self.available_agents.clear()
        self._load_all_agents()
