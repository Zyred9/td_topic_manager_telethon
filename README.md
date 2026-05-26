# TG 小号矩阵管控平台(Python + Telethon)

把原 Java + TDLib 版迁移到 **Python + Telethon + FastAPI + MySQL**,单进程多 client。
后端接口严格对齐现有前端 `td_topic_manager_web`(前端不改)。

## 功能

- **登录小号**:协议号 zip 批量登录(`.session` + 自带 `.json` 凭证)/ 手机号 + 验证码 + 2FA 登录
- **小号列表**:批量加群/退群/在群校验/删除;单号编辑资料、定时发送、关键字回复、AI 人设
- **AI 话题**:小号自驱互聊 + 概率应答真人(deepseek 驱动,汉字数控制 + 附和短句)
- **账号管理**:后台账号 CRUD(JWT,仅超级管理员)

## 技术栈

| 维度 | 选择 |
|---|---|
| Web | FastAPI + uvicorn |
| TG | Telethon(MTProto user client),单进程多 client 共享 asyncio loop |
| DB | MySQL + pymysql(原生 SQL,无 ORM) |
| 鉴权 | JWT(PyJWT) |
| LLM | deepseek(OpenAI 兼容) |
| 节流/批次/锁 | 进程内内存(不依赖 Redis) |

## 运行

> 后端统一用 **conda 虚拟环境**管理,避免污染系统 Python、隔离依赖。

```bash
# 1. 创建并激活 conda 虚拟环境(Python 3.10~3.13;本项目验证于 3.13)
conda create -n td_topic python=3.13 -y
conda activate td_topic

# 2. 安装依赖(在已激活的虚拟环境内)
pip install -r requirements.txt

# 3. 配置(复制并按需修改)
cp .env.example .env
#   - DB_HOST/PORT/USER/PASSWORD/NAME:MySQL 连接(库不存在会自动建)
#   - TG_API_ID/HASH:手机号新登录用的全局凭证(协议号用各自 .json)
#   - LLM_API_KEY:deepseek key
#   - JWT_SECRET:改成随机串
#   - INIT_ADMIN_USER/PASSWORD:首次启动自动创建超管(首登请改密)

# 4. 启动(自动建库建表 + 建默认超管 + 全起已登录小号)
#    确保已 conda activate td_topic
python main.py
```

> 日常每次启动前先 `conda activate td_topic` 再 `python main.py`。
> 后续依赖更新:`conda activate td_topic && pip install -r requirements.txt`。

服务默认监听 `0.0.0.0:8813`,根路径 `/collect`(匹配前端 vite 代理 `/api → localhost:8813/collect`)。
接口文档:`http://localhost:8813/collect/docs`。

### Linux 后台常驻(conda + systemd)

服务器上让后端开机自启 + 崩溃重启,systemd 直接指向 conda 环境里的 python(无需先 activate):

```ini
# /etc/systemd/system/td-backend.service
[Unit]
Description=TD Topic Manager (Python/Telethon) Backend
After=network.target mysqld.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/td
# 指向 conda 环境的 python 绝对路径(用 `conda env list` 查路径)
ExecStart=/root/miniconda3/envs/td_topic/bin/python /opt/td/main.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/opt/td/backend.log
StandardError=append:/opt/td/backend.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now td-backend
sudo systemctl status td-backend       # 查状态
tail -f /opt/td/backend.log            # 实时日志
```

## 前端联调

前端 `td_topic_manager_web` 开发模式下 `npm run dev`,vite 已配置 `/api` 代理到 `localhost:8813/collect`,直接登录默认超管即可。

## 协议号 zip 格式

每个号一个目录(目录名 = 手机号):
```
<zip>/
  +8613800000001/
    +8613800000001.session   ← Telethon session(必需)
    +8613800000001.json      ← 自带 app_id/app_hash/device/app_version/twoFA(强烈建议)
    2fa.txt                  ← 二级密码(可选,= json.twoFA)
    tdata/                   ← TDLib 残留,忽略
```
连接协议号时使用其自带 `.json` 的 app_id/设备指纹,与 session 创建时一致以防封号;
缺 `.json` 时回退全局凭证(有失效风险)。

## 目录结构

```
config/        配置(.env)、常量、内置人设模板
infra/         db(连接+建表)、telethon_factory(client 构造)
core/          client_manager / message_sender / throttle / batch_store / update_router / lifecycle
services/      account / import / group_op / schedule / keyword / topic / topic_scheduler / ai_chat_engine / persona / auth / admin
repositories/  各表数据访问(原生 SQL)
entities/      dataclass 实体
api/           FastAPI 路由(对齐前端契约)+ deps(Result/JWT)+ schemas
helpers/       han_counter / td_error / link_parser
sql/schema.sql 建表脚本(启动幂等执行)
sessions/      {phone}.session
data/avatar/   头像(StaticFiles 服务)
data/upload/   zip 上传临时目录
```

## 风控

- 同号最小发言间隔 1.5s、每号每分钟全网 ≤8 条(跨话题/功能累计)
- 加群间随机 5~15s 抖动;FloodWait 自动退避重试
- 跨功能配额优先级:关键字 > 应答真人 > 自驱 > 定时
- AI 回复仅统计汉字,硬上限 50 字;自驱按概率发附和短句,应答真人强制走 LLM
- 服务重启不自动恢复任务(定时/话题置停),由运营手动重启

## 注意

- 单进程多 client 共享一个 event loop;同步 DB 调用经线程池(`run_db`)避免阻塞
- 代理暂未启用,需要时在 `.env` 填 `TD_PROXY_HOST/PORT`(SOCKS5)
