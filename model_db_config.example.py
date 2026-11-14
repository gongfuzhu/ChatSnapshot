# MongoDB Atlas 连接配置示例。
# 复制为 model_db_config.py 并填入真实连接串（该文件已在 .gitignore，不会提交）。
# 连接串在 Atlas「Database → Connect → Drivers」获取，形如：
# mongodb+srv://<user>:<password>@clusterchat.pp1vdqg.mongodb.net/?retryWrites=true&w=majority&appName=ClusterChat
MONGO_URI = "mongodb+srv://<user>:<password>@clusterchat.pp1vdqg.mongodb.net/?retryWrites=true&w=majority&appName=ClusterChat"
MONGO_DB = "clusterchat"
MONGO_COLLECTION = "model_videos"
