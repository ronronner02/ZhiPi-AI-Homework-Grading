# 智批π Demo 公网部署

把 Demo 挂成一个「发给评委/老师，点开就能用」的链接。全程约 10 分钟。

本目录下的所有内容都在本机 Docker 29.5 上实测跑通过：镜像构建、容器启动、
四道风控闸、只读根文件系统、时区、四屏动线。下文标注「已实测」的即为此次验证覆盖。

---

## 一、五分钟上线

在一台有公网 IP 的 Linux 服务器上（阿里云轻量应用服务器 2C2G 足够）：

```bash
# 1. 装 Docker（已装可跳过）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2. 拉代码
git clone <你的仓库地址> && cd zhipi-submission/deploy

# 3. 配置（至少填访问口令）
cp .env.example .env
vi .env          # ZHIPI_ACCESS_CODE=你自己定的口令

# 4. 一键部署（自检 → 构建 → 启动 → 冒烟测试）
bash deploy.sh
```

脚本跑完会直接打印可以转发的链接。**最后一步别忘了**：去云控制台的
安全组 / 防火墙放行对应 TCP 端口（默认 8010），否则本机自测通过但外网连不上——
这是这类部署最常见的「明明成功了却打不开」。

```bash
# 阿里云/腾讯云：控制台 → 安全组 → 添加入站规则 → TCP 8010 → 0.0.0.0/0
# 若服务器上还开了 ufw：
sudo ufw allow 8010/tcp
```

---

## 二、分享方式

```
http://<公网IP>:8010/?code=<你的口令>
```

服务端校验口令后会 **303 跳转**到 `/`，把明文口令从地址栏抹掉，
同时写一个 `HttpOnly` Cookie（有效期 7 天）。所以：

- 链接可以随便转发，转发者不必解释「口令是多少」；
- 但收到链接的人截图时，地址栏里不会留着口令；
- 没有口令直接访问 `/` 会看到口令输入页，访问 `/api/*` 会拿到 401 JSON。

**已实测**：`/healthz` 免口令 200；无口令 `/api/demo/config` → 401；
错误口令 → 401;  正确口令 → 303 且响应头带 `Set-Cookie: zhipi_pass=...; HttpOnly`。

---

## 三、常用运维命令

```bash
cd deploy
docker compose logs -f          # 看实时日志
docker compose ps               # 看状态与健康检查
docker compose restart          # 改了 .env 后重启生效
docker compose down             # 停止并删除容器
git pull && bash deploy.sh      # 更新代码并重新部署
```

---

## 四、配置说明

完整清单见 `.env.example`，每一项都有中文注释。这里只讲最需要决策的四条。

### 1. 访问口令 `ZHIPI_ACCESS_CODE`

公网必填。留空则任何人都能打开。

### 2. 要不要配真实模型 Key

| 配置 | 体验者能做什么 | 成本 |
| --- | --- | --- |
| 都不配（默认） | 点内置 11 份样例照片，走完识别→批改→终审→看板全动线 | 零 API 消耗，可断网 |
| 配 `ZHIPI_VLM_API_KEY` | 额外可以**上传自己手写的作业照片**并被真实识别 | 按调用量计费 |
| 再配 `ZHIPI_LLM_API_KEY` | 上传件的批改也走真实大模型而非规则引擎 | 按调用量计费 |

配了 Key 就**必须**设日配额，见下条。

### 3. 日配额 `ZHIPI_VLM_DAILY_LIMIT` / `ZHIPI_GRADE_DAILY_LIMIT`

一个公开链接被脚本刷一晚上，足够把 API 余额打空。设一个自己能承受的数字
（示例给的 200 / 400）。**超限不会报错**：自动回落到离线样例匹配，
页面明确告知「今日真实识别额度已用尽，可点选内置样例」，红黄绿分流与
教师终审完全不受影响。`deploy.sh` 会在「配了 Key 但没设配额」时警告。

### 4. `ZHIPI_TRUST_PROXY` —— 这条最容易配错

| 部署形态 | 该设什么 | 配错的后果 |
| --- | --- | --- |
| 容器端口直接对公网 | **留空** | 设成 1 的话，任何人都能伪造 `X-Forwarded-For` 换新 IP，限流等于没有 |
| 前面有 Nginx / SLB | `1` | 留空的话，所有请求的来源 IP 都是反代地址，限流会把全部访问者算成同一个人，一个人刷满全体被锁 |

---

## 五、可选：Nginx + HTTPS

想用域名和 https（微信里分享 http 链接会被提示不安全）：

```bash
# 1. 让容器端口只对本机开放
#    .env 里改：ZHIPI_BIND=127.0.0.1
#    .env 里加：ZHIPI_TRUST_PROXY=1
docker compose up -d

# 2. Nginx 反代
sudo apt-get install -y nginx
```

```nginx
server {
    listen 80;
    server_name demo.example.com;

    location / {
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 图片上传：前端已把 >1MB 的照片压到 1600px 长边，
        # 但 Nginx 默认 client_max_body_size 是 1m，会先于应用返回 413。
        client_max_body_size 16m;
        proxy_read_timeout 120s;   # 真实 VLM 识别可能要几十秒
    }
}
```

```bash
# 3. 免费证书
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d demo.example.com
```

---

## 六、为什么是单 worker（不要随手改成 4）

`docker-compose.yml` 里写死了 `--workers 1`。这不是偷懒：

会话隔离（`pipeline/session_store.py`）、限流滑动窗口、日配额计数，
三者都是**进程内内存态**。多 worker 下同一个体验者的连续请求会落到不同进程，
表现为：

- 教师终审记录「时有时无」（写在了 A 进程，下次读到 B 进程）；
- 限流与日配额被放大成 N 倍（每个进程各算一份）。

要横向扩容，得先把这三处状态外置到 Redis，不是改一个数字的事。
当前场景（演示链接、几十人并发）单 worker 完全够用：实测空载内存约 51MiB。

---

## 七、安全边界

这是一个**接受任意人上传图片**的公网服务，所以容器做了收口，全部已实测：

| 措施 | 配置 | 实测结果 |
| --- | --- | --- |
| 非 root 运行 | `USER zhipi` | `uid=1000(zhipi)` |
| 根文件系统只读 | `read_only: true` | 写 `/app` 失败，写 `/tmp` 成功 |
| 丢弃所有 capability | `cap_drop: ALL` | `CapDrop=[ALL]` |
| 禁止提权 | `no-new-privileges` | 已设置 |
| 内存硬上限 | `768M` | `Memory=805306368` |
| 时区 | `TZ=Asia/Shanghai` | `CST +0800` |

时区不是小事：日配额按**服务器本地日期**滚动，容器默认 UTC 会让配额在
北京时间早上 8 点重置，和运营预期错开 8 小时。

应用层还有四道闸（`pipeline/guard.py`），实测：

- 请求体上限：13.3MB 载荷 → `413`，服务存活，容器内存稳定在 51MiB
  （在缓冲前就拒绝，不会被撑爆）；
- 单 IP 限流：`5/60` 配置下第 5 次起 → `429` 且带 `Retry-After`；
- 图片格式白名单 + 4000 万像素上限（挡解压炸弹与伪装文件）；
- 日配额用尽 → 自动回落离线链路，不把报错甩给体验者。

---

## 八、排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 本机 curl 通，外网打不开 | 安全组/防火墙没放行 | 云控制台放行 TCP 端口；`sudo ufw allow 8010/tcp` |
| 所有人被一起限流 | 有反代但没设 `ZHIPI_TRUST_PROXY=1` | 设为 1 后 `docker compose restart` |
| 限流形同虚设 | 没有反代却设了 `ZHIPI_TRUST_PROXY=1` | 清空该项 |
| 上传照片返回 413 | Nginx `client_max_body_size` 默认 1m | 按第五节调到 16m |
| 配额早上 8 点重置 | 容器时区是 UTC | 确认 `TZ=Asia/Shanghai`（compose 已内置） |
| 终审记录时有时无 | worker 数被改成 >1 | 改回 `--workers 1`，见第六节 |
| `docker: permission denied` | 当前用户不在 docker 组 | `sudo usermod -aG docker $USER` 后重新登录 |

查当前生效的风控配置（需带口令 Cookie）：

```bash
curl -s -c j.txt "http://127.0.0.1:8010/?code=<口令>" -o /dev/null
curl -s -b j.txt http://127.0.0.1:8010/api/demo/config
```

---

## 九、本地起（不用 Docker）

```bash
cd demo
pip install -r requirements.txt
python app.py            # http://127.0.0.1:8010
```

不配任何环境变量时风控全部关闭、走离线规则引擎，行为与部署前完全一致。
Windows 上可直接双击 `run_demo.bat`。
