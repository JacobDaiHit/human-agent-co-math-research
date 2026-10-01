# 离线代码沙箱

仅运行隔离容器内的 Python；可信镜像现在加入固定版本 SymPy，用于符号运算。2026-09-13 修复 Windows 启动配置并恢复 Docker 后，真实隔离验收 3 项全部通过；记录见 `docs/acceptance/sandbox-isolation-2026-09-13.xml`。不得以宿主 Python 替代。

环境恢复后，在项目根目录构建可信镜像（只把本目录作为构建上下文，绝不把项目根目录、`.env` 或评测材料复制进镜像）：

```powershell
docker build --tag mathagent-offline:local sandbox
$sandboxImageId = docker image inspect --format '{{.Id}}' mathagent-offline:local
$env:MATHAGENT_SANDBOX_IMAGE = $sandboxImageId
$env:MATHAGENT_TEST_CODE_SANDBOX = '1'
.venv\Scripts\python.exe -m pytest tests/integration/test_code_sandbox_isolation.py
```

构建基础镜像可能需要网络，这属于准备依赖。模型实际执行使用固定本地 sha256 镜像、`--pull never` 与 `--network none`，没有宿主文件挂载，不转发 API 密钥环境。持久配置只记录镜像 ID；启动 API 时继承该配置，再在项目运行设置中显式启用。

每次最多 10 秒、256 MiB 内存、1 CPU、32 个进程、每路输出 32 KiB；工作目录为 16 MiB 临时文件系统，单文件写入限制 1 MiB。没有项目终身 32 次的计算上限。模型请求预算另外计算，不因代码执行而增加。

可信父进程以容器 root 监督，用户代码降为 UID/GID 65534，无补充组；禁止新权限。父进程在超时或输出溢出后退出，即使子进程逃离进程组也由容器退出结束。没有一般终端、包安装或联网工具。

`sandbox-jobs` 位于数据库旁，保存执行回执。相同项目、执行标识、代码、镜像、时限和工具版本使用同一回执；连续研究的执行标识对应实际模型工具请求，恢复时不会重复计算。未知回执不自动重跑。永久删除会清除代码与输出并保留执行回执和指纹。

程序输出是计算观察，不能单独使一般命题获得完整证明或形式化标签。更换镜像或隔离实现后，须重新通过真实隔离测试，才能运行带沙箱的付费 IMO benchmark。

本轮 Docker 后端未就绪，因此新镜像中的 SymPy 及实际隔离测试仍待运行；上文的 2026-09-13 结果仅为旧镜像历史，不作为本轮通过证据。计算在数据库写入事务外执行，工作台可以同时保存研究材料。
