# 校园失物招领 · 云端版部署指南（保姆级）

这套代码是一份**可以真上线**的最小产品：登记走大模型理解自然语言，查询走本地内存索引。

## 0. 先看实测结果（用真实 Key 跑过）

```text
健康检查：大模型就绪 = True | 模型 = glm-4-flash
登记：昨天下午在图书馆二楼…黑色的长柄雨伞，伞柄有点磨损
  来源=llm  耗时=3884 ms
  标题=雨伞  类别=雨伞  颜色=黑色  地点=图书馆二楼  特征=['长柄', '伞柄磨损']
登记：在三食堂捡到一个蓝色的保温杯…我电话13812345678
  来源=llm  耗时=3265 ms  联系自动脱敏为 138****5678
查询「黑色 雨伞 图书馆」：服务端 3.0 ms / 端到端 10.0 ms / 扫描 7 条
  命中：雨伞(98.7)、黑色长柄雨伞(78.7)、黑色双肩包(24.7)
```

结论：登记走大模型约 3–4 秒，查询稳定在个位数毫秒。

## 0.1 你会得到什么

| 能力 | 接口 | 技术 | 耗时 |
|---|---|---|---|
| 登记失物/招领 | POST /api/register | 智谱 GLM-4-Flash 抽取结构化字段 → 写 JSON | 2–6 秒 |
| 查找物品 | GET /api/query?q= | 纯 Python 内存关键词打分，不碰大模型 | 1–10 毫秒 |
| 健康检查 | GET /api/health | 服务状态、已收录条数、大模型是否就绪 | 毫秒 |
| 最近登记 | GET /api/items?limit=20 | 分页读 JSON | 毫秒 |

设计分工是刻意的：**贵且慢**的大模型只在登记的写入路径上跑一次，**查询**永远是本地索引，所以并发再多也不会被模型限流拖死。

---

## 1. 先准备三样东西

### 1.1 智谱 API Key（免费）

> ⚠ 拿到 Key 之后：**本地放进 `.env`（已被 .gitignore 屏蔽），云平台放进环境变量**。
> 用 GitHub 网页「Upload files」上传时，**不要勾选 `.env`** —— 网页上传不看 .gitignore，选什么传什么。
> 如果 Key 曾经出现在聊天记录、截图、比赛材料里，记得在控制台重新生成一个。


1. 打开 https://open.bigmodel.cn/ ，用手机号注册；
2. 完成实名认证（不实名调不了免费模型）；
3. 左侧「API Keys」→「创建 API Key」→ 复制那一长串 Key。
   **注意：Key 只显示一次，先粘到记事本里。**
4. 模型用 ```glm-4-flash```（本项目的默认值），它属于免费模型；额度以控制台「资源包/用量」页为准。

> 安全提醒：Key 只能放在环境变量里，**绝不能写进代码、不能提交到 GitHub**。本项目只从 ```ZHIPU_API_KEY``` 环境变量读。

### 1.2 GitHub 账号

https://github.com/signup 注册，记下用户名和密码。后面云平台全部靠它拉代码。

### 1.3 这份代码

目录：```C:\deepseek\xiaoyuanqiang\cloud_lostfound```，包含：

```
app.py              Flask 主程序（路由 + 限流 + 脱敏）
zhipu_client.py     智谱调用 + 失败自动降级到本地规则
matcher.py          毫秒级关键词检索
store.py            JSON 存储（原子写 + 线程锁）
templates/index.html  前端页面
requirements.txt    依赖清单
Dockerfile / Procfile / render.yaml  三种平台用得到的配置
.env.example        环境变量样例
```

---

## 2. 本地先跑通（强烈建议，10 分钟）

云平台出问题时你会抓瞎，所以先在本地确认代码是好的。

### 2.1 装 Python

https://www.python.org/downloads/ 装 3.11 或 3.12，安装时勾选 **Add python.exe to PATH**。

### 2.2 装依赖

在文件资源管理器地址栏输入 ```cmd``` 回车，进到项目目录，然后：

```bat
cd /d C:\deepseek\xiaoyuanqiang\cloud_lostfound
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

看到 ```Successfully installed Flask-3.0.3 requests-2.32.3 gunicorn-22.0.0``` 就成了。

### 2.3 配 Key 并启动

cmd 窗口里（注意 ```set``` 只在当前窗口有效）：

```bat
set ZHIPU_API_KEY=把刚才复制的Key粘在这里
python app.py
```

> 如果提示端口被占用：先 `set PORT=5050` 再 `python app.py`，然后访问 http://127.0.0.1:5050 。

浏览器打开 http://127.0.0.1:5000 ，顶部应显示「服务在线 · 已收录 5 条 · **大模型已就绪**」。
如果显示「大模型未配置（走本地规则）」，说明环境变量没生效 → 关掉窗口重来。

再验证一次接口：

```bat
curl http://127.0.0.1:5000/api/health
```

返回里 ```llm_ready``` 为 ```true``` 就没问题。

---

## 3. 把代码传到 GitHub（网页上传，不用命令行）

1. 登录 GitHub → 右上角 **+** → **New repository**；
2. Repository name 填 ```campus-lostfound``` → 选 **Public**（私有仓库在免费云平台可能需要额外授权）→ **Create repository**；
3. 页面点 **uploading an existing file**；
4. 把 ```cloud_lostfound``` 文件夹里的**所有文件和子文件夹**拖进去：
   - 一定要拖：```app.py``` ```zhipu_client.py``` ```matcher.py``` ```store.py``` ```requirements.txt``` ```templates/```（文件夹）
   - 别拖：```.venv```、```data/items.json```、```.env```
5. 底部 **Commit changes**。

上传后在仓库首页确认能看到 ```app.py``` 和 ```templates/```，点进 ```templates``` 能看到 ```index.html```。

---

## 4. 方案 A：Render（最省心，先看这个）

### 4.1 注册

1. 打开 https://render.com/ → **Get Started** → 用 **GitHub 账号**登录（省掉邮箱验证）；
2. 授权 Render 读取你的仓库。

### 4.2 建服务

1. 控制台点 **New +** → **Web Service**；
2. **Connect** 你的 ```campus-lostfound``` 仓库；
3. 按下表填：

| 字段 | 填什么 |
|---|---|
| Name | campus-lostfound |
| Region | Singapore（离国内最近） |
| Branch | main |
| Runtime | **Python 3** |
| Build Command | ```pip install -r requirements.txt``` |
| Start Command | ```gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60``` |
| Instance Type | **Free** |

4. 往下展开 **Environment Variables**，加两条：

| Key | Value |
|---|---|
| ```ZHIPU_API_KEY``` | 你的 Key |
| ```ZHIPU_MODEL``` | ```glm-4-flash``` |

> 启动命令里为什么是 `--workers 1 --threads 8`：当前数据存在单个 JSON 文件里，
> 多进程会互相覆写；单进程 + 8 线程既能抗住并发（查询是内存操作，不占线程多久），又保证数据一致。
> 以后换成云数据库，再改成 `--workers 4` 即可。

5. 点 **Deploy Web Service**，等日志出现 ```Booting worker``` 和 ```Listening at```。
6. 顶部会给出地址：```https://campus-lostfound-xxxx.onrender.com``` —— **这就是你的永久公网链接**。

### 4.3 Render 免费版必须知道的两件事

1. **会休眠**：15 分钟没人访问就挂起，下次访问冷启动 30–60 秒。
   对策：注册 https://uptimerobot.com/（免费），建一个 HTTP(s) 监控，每 10 分钟访问一次 ```https://你的域名/api/health```，就不会睡着了。比赛当天提前 5 分钟打开一次页面即可。
2. **磁盘是临时的**：重新部署/重启后 ```data/items.json``` 会回到初始状态。
   对策见第 8 节。

---

## 5. 方案 B：Zeabur（亚洲节点，国内访问更快）

1. 打开 https://zeabur.com/ → 用 GitHub 登录；
2. **Create Project** → 选一个区域（Hong Kong / Tokyo）；
3. 项目里点 **Add Service** → **Git** → 选 ```campus-lostfound``` 仓库；
4. Zeabur 会自己识别出 Python 项目，如果没识别，手动把启动命令设为
   ```gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60```；
5. 进入服务的 **Variables**，加 ```ZHIPU_API_KEY```（和可选的 ```ZHIPU_MODEL=glm-4-flash```）；
6. 服务的 **Networking** → **Generate Domain** → 得到 ```https://xxx.zeabur.app```；
7. 打开该域名，看到首页即成功。

> Zeabur 是免费额度制（每月有额度上限，超出会停服），比赛用完全够；额度用尽时到 Billing 页看用量。

---

## 6. 方案 C：Hugging Face Spaces（完全免费、不绑卡，Docker 部署）

适合「不想绑任何支付方式」的情况。

1. 注册 https://huggingface.co/ ；
2. 右上头像 → **New Space** → 名字 ```campus-lostfound``` → **SDK 选 Docker** → **Blank** → Public → Create；
3. 把项目文件传上去（Files → Add file → Upload files），**注意 Dockerfile 要改一处**：

```dockerfile
ENV PORT=7860
EXPOSE 7860
```

4. Space 页面 → **Settings** → **Variables and secrets** → 加 Secret：```ZHIPU_API_KEY```；
5. 等构建完成，地址形如 ```https://用户名-campus-lostfound.hf.space```。

> HF Space 会休眠（免费），第一次访问同样要等几十秒。

---

## 7. 部署完必须做的三步验收

1. 打开 ```https://你的域名/api/health``` ，确认 ```llm_ready 为 true```、```items``` 有数字；
2. 打开首页 → 「登记失物」标签 → 粘一段话：

```
昨天下午在图书馆二楼自习，走的时候把一把黑色的长柄雨伞落在座位上了，伞柄有点磨损
```

   点提交，3–6 秒后应出现绿色标签「类别：雨伞 / 颜色：黑色 / 地点：图书馆二楼」和一枚 **大模型抽取** 的绿色徽章；
3. 切到「查找我的东西」→ 输入 ```黑色 雨伞 图书馆``` → 应该秒出卡片，状态栏显示「用时 1.x 毫秒」。

三步全过，你的作品就真的在线了，把链接做成二维码给评委扫。

### 想用一条命令全自动验收？

项目里带了自检脚本，部署完成后在本地跑一次即可（把地址换成你的域名）：

```bat
.venv\Scripts\activate
python tests\smoke_test.py https://你的域名
```

它会依次打健康检查、登记一条、查两次、列一次，并打印「来源=llm」还是「来源=rule」。
**看到 来源=llm 就说明大模型真的接上了**；如果是 rule，回第 9 节第一条排查。

样例输出：

```text
[1/4] 健康检查      ok=True 已收录=5 模型=glm-4-flash 大模型就绪=True
[2/4] 登记          黑色长柄雨伞 / 雨伞 / 黑色 / 图书馆二楼  来源=llm  耗时=2843 ms
      脱敏后的详情：地点：图书馆二楼 · 特征：长柄、伞柄有磨损 · 联系：138****5678
[3/4] 查询「黑色 雨伞 图书馆」 扫描 6 条，服务端 0.9 ms，端到端 12.4 ms → 黑色长柄雨伞(100)
[4/4] 列表          总数=6
全部通过 ✅
```

---

## 8. 免费版的两个坑，提前知道不慌

### 坑 1：数据会丢（文件系统是临时的）

```data/items.json``` 在 Render / Zeabur 免费实例里随容器重建而消失。

三个解决办法，按省事程度排序：

1. **比赛期先忍着**：演示前重新登记几条即可，功能完整、不影响评分；
2. **换成免费云数据库**（推荐长期做）：把 ```store.py``` 换成 Turso（SQLite 云版）、Supabase（Postgres）或 Upstash（Redis），接口不用改，只换存储层；
3. Render 付费挂盘（Disk，$0.25/GB/月）：在服务 **Disks** 里挂一个挂载点 ```/var/data```，再把环境变量 ```DATA_PATH``` 设为 ```/var/data/items.json```。

### 坑 2：冷启动

免费实例闲置会休眠，第一次访问要等 30–60 秒。比赛前 5 分钟打开一次页面，或按 4.3 配一个 UptimeRobot 探活。

---

## 9. 常见报错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面能开，登记一直「本地规则抽取」 | 没读到 Key | 平台环境变量里加 ```ZHIPU_API_KEY``` 后 **重新部署**（改环境变量不会自动重启进程时手动 Manual Deploy） |
| 返回 401 / auth_failed | Key 错了或未实名 | 控制台重新生成 Key，确认实名通过 |
| 返回 429 | 免费额度用尽或请求太快 | 换模型 ```glm-4-air```，或降低登记频率（本项目已内置 12 次/分钟限流） |
| 502 Bad Gateway | 启动命令不对 | 确认 Start Command 用的是 ```gunicorn app:app --bind 0.0.0.0:$PORT``` |
| 日志里 ```ModuleNotFoundError: No module named flask``` | 依赖没装 | Build Command 必须是 ```pip install -r requirements.txt``` |
| 页面 404 / TemplateNotFound | ```templates/``` 没上传 | 仓库里必须有 ```templates/index.html``` |
| 本地 ```gunicorn 不是内部或外部命令``` | Windows 不支持 gunicorn | 本地用 ```python app.py```，gunicorn 只在云上用 |
| 登记超时（>20 秒） | 网络到智谱慢 | 把 ```ZHIPU_TIMEOUT``` 设成 30；或部署到香港/新加坡节点 |

---

## 10. 上线到「真能运营」还差什么

现在的版本足够比赛，但如果真要长期运营，建议按顺序补：

1. **数据持久化**（第 8 节）——这是“重启丢数据”的硬伤；
2. **登录与归属**：让失主用校园账号登录，才能认领自己发布的信息；
3. **认领核验流程**：现在是「信息展示」，真实运营需要「认领申请 → 管理员核验 → 交付」；
4. **隐私合规**：本项目已做基础脱敏（手机号留前 3 后 2、姓名首字 + 星号），正式运营建议加《隐私政策》与数据删除入口；
5. **查询再升级**：记录多了以后把内存索引换成 SQLite FTS5 或 Meilisearch，仍是毫秒级；
6. **限流与防风控**：本项目内置了简单的每 IP 每分钟 12 次登记限流，正式上线建议换成 Redis 计数。

---

## 附：三种部署方式怎么选

| | Render | Zeabur | HF Spaces |
|---|---|---|---|
| 免费额度 | 750 小时/月 | 月度额度 | 免费 |
| 需要绑卡 | 不需要 | 视账号而定 | 不需要 |
| 国内访问速度 | 一般 | 较快 | 一般 |
| 冷启动 | 15 分钟闲置休眠 | 有休眠策略 | 有休眠 |
| 自定义启动命令 | 支持 | 支持 | Dockerfile 控制 |

比赛建议：**主用 Render，备用 Zeabur**，两个都部署一遍，现场哪个快用哪个，把域名二维码提前打印出来。
