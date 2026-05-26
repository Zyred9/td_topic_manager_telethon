# Linux 部署指南(Python + Telethon)

> 适用:Ubuntu 20.04+ / Debian 11+ / CentOS 7+,海外服务器(能直连 Telegram)
> 架构:Nginx 反代 → 前端静态文件 + 后端 uvicorn(8813)
> 前提:服务器有 root/sudo,已装或将装 MySQL 8

```
浏览器 → Nginx (80/443)
         ├─ /         → 前端静态文件 dist/(纯静态,服务器不需要 Node)
         └─ /api/*    → 反向代理到 http://127.0.0.1:8813/collect/(uvicorn)
```

---

## 〇、要上传哪些东西

| 上传内容 | 来源 | 说明 |
|---|---|---|
| **后端源码** | `td_topic_manager_telethon/` | 整个目录,但**排除** `sessions/`、`data/`、`.env`、`__pycache__/`、`*.zip` |
| **前端静态文件** | `td_topic_manager_web/dist/` | 在开发机 `npm run build` 产出,纯静态;服务器不装 Node |
| **协议号 zip** | 你的协议号包 | 后续从网页后台上传,不用提前放服务器 |

> **关于前端要不要重新上传**:前端请求统一走相对路径 `/api`(由 Nginx 反代),代码里没写死服务器地址。
> 所以只要在开发机 `npm run build` 一次,把产出的 `dist/` 传到服务器即可。后端 IP 变了**也不用重新 build**(走 Nginx 反代,前端无感知)。

---

## 一、后端部署

### 1.1 装系统依赖(Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version    # 需 3.10+;本项目本机用 3.13,3.10~3.13 均可
```

CentOS:`sudo yum install -y python3 python3-pip`(必要时装 python3.10+)。

### 1.2 上传后端源码

开发机打包(排除运行期数据与密钥):

```bash
cd D:/open_workspace
tar czf td_backend.tar.gz \
  --exclude='td_topic_manager_telethon/sessions' \
  --exclude='td_topic_manager_telethon/data' \
  --exclude='td_topic_manager_telethon/.env' \
  --exclude='td_topic_manager_telethon/__pycache__' \
  --exclude='td_topic_manager_telethon/.omc' \
  --exclude='*.zip' \
  td_topic_manager_telethon

scp td_backend.tar.gz root@your-server.com:/tmp/
```

服务器解压到 `/opt/td`:

```bash
sudo mkdir -p /opt/td
sudo tar xzf /tmp/td_backend.tar.gz -C /opt/td --strip-components=1
cd /opt/td
```

### 1.3 创建虚拟环境 + 装依赖

```bash
cd /opt/td
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 1.4 装 MySQL(若未装)

```bash
sudo apt install -y mysql-server
sudo systemctl enable --now mysql
# 设置 root 密码 / 建专用账号(示例用 root)
sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY 'your-db-password';"
```
库不用手动建,后端启动会自动 `CREATE DATABASE IF NOT EXISTS` 并建表。

### 1.5 配置 .env

```bash
cd /opt/td
cp .env.example .env
vi .env
```

至少改这几项:

```bash
DB_PASSWORD=your-db-password           # 改成服务器 MySQL 实际密码
JWT_SECRET=$(openssl rand -hex 32)     # 必须改成随机串
INIT_ADMIN_PASSWORD=改个强密码          # 首次启动建超管用,登录后再改
LLM_API_KEY=sk-xxxx                    # deepseek key(已有默认值,确认有效)
# 海外服务器直连,代理留空即可:
# TD_PROXY_HOST= / TD_PROXY_PORT=  保持注释
```

### 1.6 先手动跑一次验证

```bash
cd /opt/td
source venv/bin/activate
python main.py
# 看到 "服务启动完成,根路径 /collect,端口 8813" 即 OK
# 另开终端验证:
curl http://127.0.0.1:8813/collect/openapi.json    # 返回 JSON 即后端正常
# Ctrl+C 停掉,改用下面的 systemd 守护
```

### 1.7 systemd 守护(开机自启 + 崩溃重启)

```bash
sudo vi /etc/systemd/system/td-backend.service
```

```ini
[Unit]
Description=TD Topic Manager (Python/Telethon) Backend
After=network.target mysql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/td
# venv 里的 python,会自动读取 /opt/td/.env
ExecStart=/opt/td/venv/bin/python /opt/td/main.py
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
sudo systemctl status td-backend          # 查状态
tail -f /opt/td/backend.log               # 实时日志
```

> 注意:**服务重启后所有定时任务/AI 话题自动置停**(需求设计),需运营登录后台手动重启任务。
> 小号(已登录的 session)会在启动时自动全起重连。

---

## 二、前端部署

### 2.1 开发机打包

> 服务器**不需要装 Node**,只在开发机打包成静态文件。

```bash
cd D:/open_workspace/td_topic_manager_web
npm install                 # 仅第一次;国内慢可先 npm config set registry https://registry.npmmirror.com
npm run build               # 产出 dist/(约 5MB),含 index.html + assets/
```

> 前端 `src/utils/request.ts` 的 baseURL 是 `/api`(相对路径),生产靠 Nginx 反代,**无需改任何后端地址**。
> `vite.config.ts` 里的 proxy 只在本地 `npm run dev` 生效,生产不用管。

### 2.2 上传 dist

```bash
cd D:/open_workspace/td_topic_manager_web
tar czf td_web.tar.gz -C dist .
scp td_web.tar.gz root@your-server.com:/tmp/

# 服务器
sudo mkdir -p /var/www/td_web
sudo tar xzf /tmp/td_web.tar.gz -C /var/www/td_web
# Ubuntu 权限:
sudo chown -R www-data:www-data /var/www/td_web
# CentOS 用 nginx 用户:sudo chown -R nginx:nginx /var/www/td_web
```

### 2.3 装并配置 Nginx

```bash
sudo apt install -y nginx        # CentOS: sudo yum install -y nginx
sudo systemctl enable --now nginx
sudo vi /etc/nginx/conf.d/td_web.conf
```

```nginx
server {
    listen 80;
    server_name your-server.com;        # 改成你的域名或服务器 IP

    root /var/www/td_web;
    index index.html;

    # 全局上传体积(zip 协议号包,默认 1MB 会卡死上传)
    client_max_body_size 200M;

    # SPA 路由:刷新不 404
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API 反代到后端 8813,后端前缀是 /collect
    # 前端 /api/auth/login → http://127.0.0.1:8813/collect/auth/login
    location /api/ {
        proxy_pass http://127.0.0.1:8813/collect/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 200M;
        proxy_read_timeout 300s;        # zip 解压/批量登录等长操作
        proxy_send_timeout 300s;
    }

    # 头像等后端静态资源(FastAPI StaticFiles 挂在 /collect/static/avatar)
    # 前端若用相对 /api 访问头像也会经上面的 /api/ 反代,无需单独配置。

    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }
}
```

**关键三点(漏一个就出问题)**:
1. `try_files ... /index.html` —— SPA 刷新不白屏
2. `proxy_pass http://127.0.0.1:8813/collect/` —— 末尾 `/collect/` 的斜杠不能漏
3. `client_max_body_size 200M` —— 不写 zip 上传报 413

```bash
sudo nginx -t           # 语法检查
sudo nginx -s reload    # 重载
```

放行防火墙 + 云控制台安全组开 80/443:
```bash
sudo ufw allow 80,443/tcp           # Ubuntu
# CentOS: sudo firewall-cmd --permanent --add-service={http,https} && sudo firewall-cmd --reload
```

### 2.4 验证

```bash
curl http://your-server.com/                      # 返回前端 HTML
curl http://your-server.com/api/openapi.json      # 经反代返回后端 JSON
```
浏览器开 `http://your-server.com` → 登录页,用 `.env` 里 `INIT_ADMIN_USER/PASSWORD` 登录,**首次登录立即改密**。

### 2.5 升级前端(后续发版)

```bash
# 开发机
cd D:/open_workspace/td_topic_manager_web && npm run build
tar czf td_web.tar.gz -C dist .
scp td_web.tar.gz root@your-server.com:/tmp/
# 服务器
sudo rm -rf /var/www/td_web/* && sudo tar xzf /tmp/td_web.tar.gz -C /var/www/td_web
sudo chown -R www-data:www-data /var/www/td_web
# 不用重启 Nginx,浏览器 Ctrl+F5 刷新
```

---

## 三、HTTPS(生产推荐)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-server.com      # 按提示选 redirect HTTP→HTTPS
sudo certbot renew --dry-run                 # 验证自动续期
```

---

## 四、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 502 Bad Gateway | 后端没起:`systemctl status td-backend`、`curl 127.0.0.1:8813/collect/openapi.json` |
| 刷新页面 404/白屏 | Nginx 缺 `try_files ... /index.html` |
| zip 上传 413 | Nginx 缺 `client_max_body_size 200M` |
| 小号全部连不上 Telegram | 服务器网络不通 Telegram。海外机一般 OK;若不通在 `.env` 配 `TD_PROXY_HOST/PORT`(SOCKS5)并重启后端 |
| 后端起不来报 DB 错 | `.env` 的 DB_PASSWORD 不对,或 MySQL 没起 |
| `pip install` 编译失败 | 装 `python3-dev gcc`(个别依赖需编译) |
| 重启后任务不跑了 | 设计如此:定时/话题重启置停,运营登录后台手动重启 |

---

## 五、目录速记

```
/opt/td/                        后端源码
/opt/td/venv/                   Python 虚拟环境
/opt/td/.env                    配置(密钥,勿外泄)
/opt/td/sessions/               小号 .session(运行期生成,备份重点)
/opt/td/data/avatar/            头像
/opt/td/data/upload/            zip 上传临时
/opt/td/backend.log             后端日志
/var/www/td_web/                前端静态文件
/etc/nginx/conf.d/td_web.conf   Nginx 配置
/etc/systemd/system/td-backend.service   后端守护
```

> **备份重点**:`/opt/td/sessions/`(小号登录态)和 MySQL 数据库。丢了 sessions 所有小号要重新登录。

---

**部署完第一件事**:登录后台 → 改默认管理员密码。
