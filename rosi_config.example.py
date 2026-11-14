# rosi(jinyemimi) 归档抓取配置示例。
# 复制为 rosi_config.py 并填入真实 PHPSESSID（该文件已在 .gitignore，不会提交）。
# PHPSESSID 在浏览器登录站点后，从开发者工具 → Cookie 中复制。
API_URL = "http://rosi.jinyemimi.com/api_beta1_3/api.php"
PHPSESSID = "你的PHPSESSID"

# 复用 model_db_config 的 Atlas 连接与库，仅集合不同
from model_db_config import MONGO_URI, MONGO_DB  # noqa: E402,F401

MONGO_COLLECTION = "rosi_archives"

# 请求默认参数（body 模板）
DEFAULT_BID = 1
DEFAULT_TYPE = "t"
PAGE_SIZE = 14          # 接口固定每页 14 条
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
