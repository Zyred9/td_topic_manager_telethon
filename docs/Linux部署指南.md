# Linux 部署指南(Python + Telethon)

> 海外服务器(能直连 Telegram)。
> 架构:**后端** uvicorn(8813) + **前端** 宝塔托管的静态站点,前端 `/api` 反代到后端 `/collect`。
> 后端代码:`git clone https://github.com/Zyred9/td_topic_manager_telethon.git`(公开仓库,无需 token)。

```
浏览器 → 宝塔 Nginx (80/443)
         ├─ /         → 前端 dist/ 静态文件(npm run build 产出)
         └─ /api/*    → 反向代理到 http://127.0.0.1:8813/collect/(uvicorn 后端)
```

---

## 全局速查:我该看哪一节?

| 场景 | 看哪节 | 一句话 |
|---|---|---|
| **后端 · 新服务器首次部署** | §一 | 装环境 → clone → 建 conda env → 配 systemd 守护 |
| **后端 · 更新代码后重启** | §二 | `git pull` → (依赖变了才 pip) → `systemctl restart td-backend` |
| **前端 · 新站点首次部署** | §三 | 装宝塔 → 建 HTML 项目(填 IP)→ 传 dist 内容 → 配 `/api` 反代 |
| **前端 · 更新发版** | §四 | 本地 `npm run build` → 覆盖站点目录文件 → Ctrl+F5 |

> **关键认知:前端不写死后端 IP**。前端请求统一走相对路径 `/api`,由宝塔 Nginx 反代到本机 8813。
> 所以后端 IP/端口变了,前端**不用重新 build**;只有前端代码本身改了才需要重新 build 上传。

---

# 一、后端 · 新服务器首次部署

> 只有**第一次**在一台新服务器上部署才做这一节(装环境 + systemd)。后续更新代码看 §二。

### 1.1 装系统依赖

```bash
# CentOS 9 / RHEL 系
sudo dnf -y update
sudo dnf install -y git wget tar bzip2 gcc

# Ubuntu / Debian 系
# sudo apt update && sudo apt install -y git wget tar bzip2 gcc
```

### 1.2 clone 后端源码(公开仓库)

```bash
git clone https://github.com/Zyred9/td_topic_manager_telethon.git /home/bots/td_topic_manager_telethon
cd /home/bots/td_topic_manager_telethon
```

> 公开仓库,直接 clone,**无需 GitHub 账号/token**。
> `sessions/`、`data/`、`.env` 在 `.gitignore` 里,不会被拉取或覆盖(小号登录态安全)。

### 1.3 创建 conda 环境 + 装依赖

> 若服务器没装 Miniconda,先装:
> ```bash
> wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
> bash Miniconda3-latest-Linux-x86_64.sh -b -p /root/miniconda3
> source /root/miniconda3/bin/activate
> ```

```bash
cd /home/bots/td_topic_manager_telethon

# 新版 conda 首次用官方源会报 "Terms of Service have not been accepted",先接受一次:
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r

conda create -n td_topic python=3.13 -y
conda activate td_topic
pip install --upgrade pip
pip install -r requirements.txt

# 记下环境里 python 的绝对路径,systemd 要用(见 1.5):
echo $CONDA_PREFIX/bin/python   # 形如 /root/miniconda3/envs/td_topic/bin/python
```

### 1.4 创建并配置 .env

`.env` 不随仓库下发(在 .gitignore 里),**新部署必须自己建**:

```bash
cd /home/bots/td_topic_manager_telethon
cp .env.example .env
vi .env
```

按服务器实际值确认/修改:

```bash
SERVER_HOST=0.0.0.0
SERVER_PORT=8813
ROOT_PATH=/collect                     # 不要改,前端 /api 反代依赖这个前缀

DB_HOST=127.0.0.1                      # 指向你已有的 MySQL
DB_PORT=3306
DB_USER=root
DB_PASSWORD=改成你 MySQL 实际密码
DB_NAME=td_topic_manager               # 库不用手动建,启动会自动 CREATE IF NOT EXISTS

JWT_SECRET=换随机串:openssl rand -hex 32
INIT_ADMIN_USER=admin
INIT_ADMIN_PASSWORD=换强密码            # 首次启动建超管用,登录后立即改
LLM_API_KEY=sk-...                     # deepseek key,确认有效

# 海外服务器直连,代理留空(保持注释):
# TD_PROXY_HOST=
# TD_PROXY_PORT=
```

> 本项目用你**已有的 MySQL**,不含 MySQL 安装。确保 MySQL 已运行、账号能连、且有建库权限。

### 1.5 先手动跑一次验证

```bash
cd /home/bots/td_topic_manager_telethon
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
WorkingDirectory=/home/bots/td_topic_manager_telethon
# 直接指 conda 环境里的 python 绝对路径(1.3 节 echo 出来的值)。
# 不用 conda run/activate:conda 在 shell 里是函数,systemd 无 shell 函数环境调不动。
ExecStart=/root/miniconda3/envs/td_topic/bin/python /home/bots/td_topic_manager_telethon/main.py
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
sudo journalctl -u td-backend -f          # 实时日志
```

> 注意:**服务重启后所有定时任务/AI 话题自动置停**(需求设计),需运营登录后台手动重启任务。
> 小号(已登录的 session)会在启动时自动全起重连。

至此后端新部署完成。后端日志走 journald,用 `journalctl -u td-backend` 查,不写文件。

---

# 二、后端 · 更新代码后重启

> 日常发版:本地改完 push 到 GitHub,服务器拉取重启。**不用再碰 §一 的环境/systemd 配置。**

开发机推代码:

```bash
# 开发机
cd D:/open_workspace/td_topic_manager_telethon
git add <改动的文件>
git commit -m "说明"
git push
```

服务器拉取并重启:

```bash
cd /home/bots/td_topic_manager_telethon
git pull

# 仅当 requirements.txt 有变更时才需要重装依赖:
# conda activate td_topic && pip install -r requirements.txt

# 重启服务生效
sudo systemctl restart td-backend
sudo journalctl -u td-backend -f          # 看日志确认起来了
```

> `sessions/`、`data/`、`.env` 在 `.gitignore` 里,`git pull` 不会动它们,小号登录态和配置安全保留。
> 重启后定时/话题任务置停,需登录后台手动重启(设计如此)。

---

# 三、前端 · 新站点首次部署(宝塔)

> 服务器**不需要装 Node**。前端在开发机打包成静态 `dist/`,通过宝塔托管。

### 3.1 开发机打包

```bash
cd D:/open_workspace/td_topic_manager_web
npm install                 # 仅第一次;国内慢可先 npm config set registry https://registry.npmmirror.com
npm run build               # 产出 dist/(含 index.html + assets/),这就是要上传的东西
```

> 前端 baseURL 是 `/api`(相对路径),生产靠 Nginx 反代,**无需改任何后端地址**。

### 3.2 装宝塔面板

服务器执行官方安装脚本(CentOS 示例,其他系统见宝塔官网):

```bash
yum install -y wget && wget -O install.sh https://download.bt.cn/install/install_6.0.sh && sh install.sh ed8484bec
```

装完记下面板地址、用户名、密码。浏览器打开面板地址登录。

> 宝塔会自带 Nginx;若提示选环境,装 **Nginx** 即可(无需 PHP/MySQL,本项目 MySQL 自备)。

### 3.3 在宝塔建 HTML 项目

1. 面板左侧 **网站** → **添加站点**。
2. **项目类型选 HTML 项目**(纯静态)。
3. **域名** 一栏:没有域名就直接填**服务器公网 IP**(如 `123.45.67.89`)。
4. 数据库、PHP 版本都选**不创建/纯静态**,提交。

建好后,宝塔会生成站点根目录,一般是 `/www/wwwroot/<你填的IP或域名>/`。

### 3.4 上传 dist 内容到站点目录

把开发机 `dist/` 目录**里面的东西**(`index.html`、`assets/` 等),传到站点根目录。

- 方式 A(面板上传):站点根目录里删掉默认的欢迎页 → 上传 `dist` 打包的 zip → 在线解压 → 确保 `index.html` 直接位于站点根目录(**不要多套一层 dist 文件夹**)。
- 方式 B(命令行):
  ```bash
  # 开发机打包
  cd D:/open_workspace/td_topic_manager_web
  tar czf td_web.tar.gz -C dist .
  # 上传到服务器后,在站点根目录解压
  tar xzf td_web.tar.gz -C /www/wwwroot/你的IP或域名/
  ```

> 校验:站点根目录下应能直接看到 `index.html`,而不是 `dist/index.html`。

### 3.5 配置 `/api` 反代(关键,漏了登录就 404)

在宝塔站点设置里加反向代理,让前端的 `/api` 打到后端 8813:

**宝塔站点 → 设置 → 反向代理 → 添加反向代理**
- 代理名称:`api`
- 目标 URL:`http://127.0.0.1:8813`
- 发送域名:`$host`
- 提交后,**编辑该反代的配置文件**,把 location 改成下面这样(核心是路径 `/api/` → 后端 `/collect/`,并放开上传体积):

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8813/collect/;   # 末尾 /collect/ 的斜杠不能漏
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 200M;       # zip 协议号包大,不写报 413
    proxy_read_timeout 300s;         # zip 解压/批量登录等长操作
    proxy_send_timeout 300s;
}
```

同时确认站点支持 **SPA 刷新不 404**。宝塔 HTML 项目通常已带 `try_files`,若刷新白屏,在站点配置的 `location /` 里补:

```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

改完在宝塔点**保存**(等价于 `nginx -t && nginx -s reload`)。

### 3.6 放行端口 + SELinux

```bash
# 1. 宝塔面板 → 安全:放行 80/443(以及面板端口)
# 2. 云服务器控制台 → 安全组:放行 80/443

# 3. CentOS 9 SELinux 必做,否则 Nginx 反代后端一律 502:
sudo setsebool -P httpd_can_network_connect 1
```

### 3.7 验证

```bash
curl http://你的IP/                       # 返回前端 HTML
curl http://你的IP/api/openapi.json        # 经反代返回后端 JSON
```

浏览器打开 `http://你的IP` → 登录页,用 `.env` 里 `INIT_ADMIN_USER/PASSWORD` 登录,**首次登录立即改密**。

---

# 四、前端 · 更新发版

> 前端代码改了才需要。后端 IP 变了**不用**重新发前端。

```bash
# 开发机重新打包
cd D:/open_workspace/td_topic_manager_web
npm run build
tar czf td_web.tar.gz -C dist .
```

把 `td_web.tar.gz` 传到服务器,在站点根目录覆盖:

```bash
# 服务器:清掉旧文件再解压新的(站点目录按你建站时的实际路径)
rm -rf /www/wwwroot/你的IP或域名/*
tar xzf /tmp/td_web.tar.gz -C /www/wwwroot/你的IP或域名/
```

不用重启 Nginx,浏览器 **Ctrl+F5** 强刷即可(清前端缓存)。

> 也可直接在宝塔文件管理器里:进站点根目录 → 全选删除 → 上传新 zip → 在线解压。

---

# 五、HTTPS(可选,生产推荐)

有域名时,宝塔一键签发最省事:

**宝塔站点 → 设置 → SSL → Let's Encrypt → 申请**,勾选「强制 HTTPS」。

无域名(纯 IP)无法签发 Let's Encrypt,走 HTTP 即可,或自行上传自签证书。

---

# 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 登录页能开,登录请求 404 | 前端 `/api` 反代没配或路径错。检查 §3.5:`proxy_pass` 必须是 `http://127.0.0.1:8813/collect/`(带末尾 `/collect/`) |
| 502 Bad Gateway | ① 后端没起:`systemctl status td-backend`、`curl 127.0.0.1:8813/collect/openapi.json`;② CentOS 9 漏了 SELinux:`sudo setsebool -P httpd_can_network_connect 1` |
| 刷新页面 404/白屏 | 站点缺 `try_files ... /index.html`(§3.5 补 location /) |
| 打开是宝塔欢迎页 | dist 没传对,或多套了一层 `dist/`。确保 `index.html` 直接在站点根目录 |
| zip 上传 413 | 反代缺 `client_max_body_size 200M`(§3.5) |
| 小号全部连不上 Telegram | 服务器网络不通 Telegram。海外机一般 OK;若不通在 `.env` 配 `TD_PROXY_HOST/PORT`(SOCKS5)并重启后端 |
| 后端起不来报 DB 错 | `.env` 的 `DB_PASSWORD` 不对,或 MySQL 没起 |
| `pip install` 编译失败 | `sudo dnf install -y gcc python3-devel`(个别依赖需编译) |
| 重启后任务不跑了 | 设计如此:定时/话题重启置停,运营登录后台手动重启 |

---

# 七、目录速记

```
/home/bots/td_topic_manager_telethon/            后端源码(git clone 来的)
/root/miniconda3/envs/td_topic/                  conda 虚拟环境(依赖)
/home/bots/td_topic_manager_telethon/.env        配置(密钥,勿外泄,不进 git)
/home/bots/td_topic_manager_telethon/sessions/   小号 .session(运行期生成,备份重点)
/home/bots/td_topic_manager_telethon/data/avatar/  头像
/home/bots/td_topic_manager_telethon/data/upload/  zip 上传临时
(后端日志走 journald,journalctl -u td-backend 查)

/www/wwwroot/<你的IP或域名>/                      前端静态文件(宝塔站点根目录)
/etc/systemd/system/td-backend.service           后端守护
```

> **备份重点**:`sessions/`(小号登录态)和 MySQL 数据库。丢了 sessions 所有小号要重新登录。

---

**部署完第一件事**:登录后台 → 改默认管理员密码。
