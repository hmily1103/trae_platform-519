import json
import os
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    def __init__(self, config_file="config.json"):
        self.config_file = config_file
        self.default_config = {
            "device_ip": "192.168.16.100",
            "device_port": "8787",
            "package_name": "com.thunder.ktv:media",
            "activity_name": "com.thunder.ktv.MainActivity",
            "server_ip": "192.168.16.210",
            "stb_ip": "192.168.16.100",
            "music_list": "7300616, 8002013, 7577121",
            "test_duration": "60",
            "interval": "5",
            "batch_loop_count": "5"
        }

    def load_config(self):
        if not os.path.exists(self.config_file):
            return self.default_config
        
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并默认配置，防止新版本加了字段但旧配置文件没有
                merged_config = self.default_config.copy()
                merged_config.update(config)
                return merged_config
        except Exception as e:
            logger.warning("加载配置文件失败: %s", e)
            return self.default_config

    def save_config(self, config_data):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            logger.info("配置已保存")
        except Exception as e:
            logger.warning("保存配置文件失败: %s", e)
