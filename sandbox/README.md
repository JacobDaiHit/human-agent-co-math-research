# 离线代码沙箱

仅运行受控容器内的 Python 标准库代码。当前 Windows 环境因 `HCS_E_HYPERV_NOT_INSTALLED` 未能通过真实隔离验收；不得以宿主 Python 替代。

环境恢复后，在项目根目录构建可信镜像（只把本目录作为构建上下文，绝不把项目根目录、`.env` 或评测材料复制进镜像）：

```powershell
docker build --tag mathagent-offline:local sandbox
$sandboxImageId = docker image inspect --format '{{.Id}}' mathagent-offline:local
$env:MATHAGENT_SANDBOX_IMAGE = $sandboxImageId
$env:MATHAGENT_TEST_CODE_SANDBOX = '1'
.venv\Scripts\python.exe -m pytest tests/integration/test_code_sandbox_isolation.py
```

构建基础镜像可能需要网络，这属于准备依赖。模型实际执行使用固定本地 sha256 镜像、`--pull never` 与 `--network none`，没有宿主文件挂载，不转发 API 密钥环境。持久配置只记录镜像 ID；启动 API 时继承该配置，再在项目运行设置中显式启用。

每次最多 10 秒、256 MiB 内存、1 CPU、32 个进程、每路输出 32 KiB；工作目录为 16 MiB 临时文件系统，单文件写入限制 1 MiB。项目总执行次数为 32，崩溃或结果未知继续占用。模型请求预算另外计算，不因代码执行而增加。

可信父进程以容器 root 监督，用户代码降为 UID/GID 65534，无补充组；禁止新权限。父进程在超时或输出溢出后退出，即使子进程逃离进程组也由容器退出结束。没有一般终端、包安装或联网工具。

`sandbox-jobs` 位于数据库旁，保存执行回执与额度。相同项目、attempt、代码、镜像、时限和工具版本使用同一回执。未知回执不自动重跑。永久删除会清除代码与输出并保留执行次数记录。

程序输出是计算观察，不能单独使一般命题获得完整证明或形式化标签。真实隔离测试通过前，不运行带沙箱的付费 IMO benchmark。
