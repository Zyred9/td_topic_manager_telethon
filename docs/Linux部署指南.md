# Linux 部署指南(Python + Telethon)

> 适用:**CentOS 9 Stream**(命令以此为准;Ubuntu/Debian 把 `dnf` 换成 `apt`、nginx 用户换 `www-data` 即可)
> 海外服务器(能直连 Telegram)
> 架构:Nginx 反代 → 前端静态文件 + 后端 uvicorn(8813)
> 前提:服务器有 root/sudo,**已有可用的 MySQL**(本项目不含 MySQL 安装,自行准备)
>
> CentOS 9 三个易踩点:① 包管理用 `dnf`;② Nginx 运行用户是 `nginx`;③ **SELinux 默认开启,会拦 Nginx 反代后端**(见 §四 Q2 必做)。

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

### 1.1 装系统依赖(CentOS 9)

```bash
sudo dnf -y update
sudo dnf install -y git wget tar bzip2 gcc
# Python 环境用 conda 管理(见 1.3),无需系统 python
```

### 1.2 拉取后端源码(git)

后端代码托管在 GitHub **私有仓库**,本地改完 push,服务器直接 `git pull` 更新,不用 scp。

```bash
# 服务器:克隆到 /home/bot/td_topic_manager_telethon(私有仓库会提示输入 GitHub 账号 + Personal Access Token)
sudo dnf install -y git
git clone https://github.com/Zyred9/td_topic_manager_telethon.git /home/bot/td_topic_manager_telethon
cd /home/bot/td_topic_manager_telethon
```

> 私有仓库认证:GitHub 已不支持密码,需用 **Personal Access Token**(GitHub → Settings → Developer settings → Personal access tokens,勾 `repo` 权限)。
> 免每次输入可配缓存:`git config --global credential.helper store`(首次输入后保存,注意 token 明文存 `~/.git-credentials`)。
>
> **注意**:`.env` 已随仓库提交(私有仓库),clone 下来就自带数据库密码/key 等配置,**无需再 cp .env.example**。若服务器 MySQL 密码与 `.env` 不同,直接改 `/home/bot/td_topic_manager_telethon/.env` 即可(改本地文件不影响 git,除非你 commit)。
> `sessions/`、`data/` 在 `.gitignore` 里,不会被拉取/覆盖(小号登录态安全保留)。

### 1.3 创建 conda 虚拟环境 + 装依赖

> 后端统一用 conda 管理环境。服务器需先装 Miniconda(若未装):
> ```bash
> wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
> bash Miniconda3-latest-Linux-x86_64.sh -b -p /root/miniconda3
> source /root/miniconda3/bin/activate
> ```

```bash
cd /home/bot/td_topic_manager_telethon

# 新版 conda 首次用官方源会报 "Terms of Service have not been accepted",
# 先接受两个官方 channel 的 ToS(一次即可,以后不再提示):
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

conda create -n td_topic python=3.13 -y
conda activate td_topic
pip install --upgrade pip
pip install -r requirements.txt

# 记下环境里 python 的绝对路径,systemd 要用(见 1.6):
which python        # 形如 /root/miniconda3/envs/td_topic/bin/python
```

### 1.4 确认 / 调整 .env

`.env` 已随仓库 clone 下来(私有仓库已含配置),通常**无需新建**。只在服务器实际值不同时调整:

```bash
cd /home/bot/td_topic_manager_telethon
vi .env
```

按需确认这几项(改本地 .env 不会被 git pull 覆盖,因为 pull 只更新已跟踪文件的远端变更;若你本地改了又不想冲突,更新前 `git stash` 一下):

```bash
DB_HOST=127.0.0.1                      # 指向你已有的 MySQL(本机或远程地址)
DB_PORT=3306
DB_USER=root
DB_PASSWORD=Qj683k2^11299%nkjb         # 改成你 MySQL 实际密码
DB_NAME=td_topic_manager               # 库不用手动建,启动会自动 CREATE IF NOT EXISTS
JWT_SECRET=...                         # 生产建议换随机串:openssl rand -hex 32
INIT_ADMIN_PASSWORD=...                # 首次启动建超管用,登录后再改
LLM_API_KEY=sk-...                     # deepseek key(已有值,确认有效)
# 海外服务器直连,代理留空即可:
# TD_PROXY_HOST= / TD_PROXY_PORT=  保持注释
```

> 本项目用你**已有的 MySQL**,不在此教安装。确保 MySQL 已运行、上面的账号密码能连、且该账号有建库权限即可。

### 1.5 先手动跑一次验证

```bash
cd /home/bot/td_topic_manager_telethon
conda activate td_topic
python main.py
# 看到 "服务启动完成,根路径 /collect,端口 8813" 即 OK
# 另开终端验证:
curl http://127.0.0.1:8813/collect/openapi.json    # 返回 JSON 即后端正常
# Ctrl+C 停掉,改用下面的 systemd 守护
```

### 1.6 systemd 守护(开机自启 + 崩溃重启)

```bash
sudo vi /etc/systemd/system/td-backend.service
```

```ini
[Unit]
Description=TD Topic Manager (Python/Telethon) Backend
After=network.target mysqld.service

[Service]
Type=simple
User=root
WorkingDirectory=/home/bot/td_topic_manager_telethon
# 用 conda run 激活 td_topic 环境再启动(环境变量齐全,比直接调 envs/.../python 规范);
# --no-capture-output 让日志实时进 journald;/root/miniconda3 换成你的 conda 安装路径(which conda 查)
ExecStart=/root/miniconda3/bin/conda run --no-capture-output -n td_topic python /home/bot/td_topic_manager_telethon/main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now td-backend
sudo systemctl status td-backend          # 查状态
sudo journalctl -u td-backend -f               # 实时日志
```

> 注意:**服务重启后所有定时任务/AI 话题自动置停**(需求设计),需运营登录后台手动重启任务。
> 小号(已登录的 session)会在启动时自动全起重连。

### 1.7 日常更新后端代码(本地 push → 服务器 pull)

开发机改完代码推到 GitHub:

```bash
# 开发机
cd D:/open_workspace/td_topic_manager_telethon
git add <改动的文件>
git commit -m "说明"
git push
```

服务器拉取并重启:

```bash
cd /home/bot/td_topic_manager_telethon
git pull
# 若依赖(requirements.txt)有变更:
conda activate td_topic && pip install -r requirements.txt
# 重启服务生效
sudo systemctl restart td-backend
sudo journalctl -u td-backend -f
```

> **.env 冲突处理**:服务器上若手动改过 `.env`,而远端 `.env` 也变了,`git pull` 会冲突。
> 处理:`git stash` → `git pull` → `git stash pop`(必要时手动解冲突);
> 或让远端为准:`git checkout -- .env && git pull`。
> `sessions/`、`data/` 在 `.gitignore` 里,`git pull` 不会动它们,小号登录态安全。

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
sudo chown -R nginx:nginx /var/www/td_web      # CentOS 9 的 Nginx 用户是 nginx
```

### 2.3 装并配置 Nginx

```bash
sudo dnf install -y nginx
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

放行防火墙 + SELinux + 云控制台安全组开 80/443:
```bash
# 1. firewalld 放行 80/443
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload

# 2. SELinux 放行 Nginx 连接后端(CentOS 9 必做,否则反代后端一律 502)
sudo setsebool -P httpd_can_network_connect 1
```
> 云服务器还要在控制台**安全组**放行 80/443。

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
sudo chown -R nginx:nginx /var/www/td_web
# 不用重启 Nginx,浏览器 Ctrl+F5 刷新
```

---

## 三、HTTPS(生产推荐)

```bash
sudo dnf install -y epel-release
sudo dnf install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-server.com      # 按提示选 redirect HTTP→HTTPS
sudo certbot renew --dry-run                 # 验证自动续期
```

---

## 四、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 502 Bad Gateway | ① 后端没起:`systemctl status td-backend`、`curl 127.0.0.1:8813/collect/openapi.json`;② CentOS 9 漏了 SELinux 放行:`sudo setsebool -P httpd_can_network_connect 1` |
| 刷新页面 404/白屏 | Nginx 缺 `try_files ... /index.html` |
| zip 上传 413 | Nginx 缺 `client_max_body_size 200M` |
| 小号全部连不上 Telegram | 服务器网络不通 Telegram。海外机一般 OK;若不通在 `.env` 配 `TD_PROXY_HOST/PORT`(SOCKS5)并重启后端 |
| 后端起不来报 DB 错 | `.env` 的 DB_PASSWORD 不对,或 MySQL 没起 |
| `pip install` 编译失败 | `sudo dnf install -y gcc python3-devel`(个别依赖需编译;conda 环境通常已自带工具链) |
| 重启后任务不跑了 | 设计如此:定时/话题重启置停,运营登录后台手动重启 |

---

## 五、目录速记

```
/home/bot/td_topic_manager_telethon/                        后端源码
/root/miniconda3/envs/td_topic/  conda 虚拟环境(依赖)
/home/bot/td_topic_manager_telethon/.env                    配置(密钥,勿外泄)
/home/bot/td_topic_manager_telethon/sessions/               小号 .session(运行期生成,备份重点)
/home/bot/td_topic_manager_telethon/data/avatar/            头像
/home/bot/td_topic_manager_telethon/data/upload/            zip 上传临时
(后端日志走 journald,用 journalctl -u td-backend 查,不写文件)
/var/www/td_web/                前端静态文件
/etc/nginx/conf.d/td_web.conf   Nginx 配置
/etc/systemd/system/td-backend.service   后端守护
```

> **备份重点**:`/home/bot/td_topic_manager_telethon/sessions/`(小号登录态)和 MySQL 数据库。丢了 sessions 所有小号要重新登录。

---

**部署完第一件事**:登录后台 → 改默认管理员密码。
