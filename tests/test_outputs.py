import subprocess, time, urllib.request, urllib.error
from pathlib import Path

# 统一使用我们在 Instruction 中明确要求的真实路径
LOG_FILE = Path("/var/log/mq-consumer/consumer.log")

def get_redis_pass():
    try:
        return Path("/etc/redis_secret").read_text().strip()
    except:
        return ""

def run_redis_cmd(*args):
    password = get_redis_pass()
    cmd = ["redis-cli"]
    if password:
        cmd.extend(["-a", password])
    cmd.extend(list(args))
    return subprocess.run(cmd, capture_output=True, text=True)

def test_01_redis_is_running_and_secured():
    """Verify that Redis is running AND enforcing password authentication."""
    res_no_auth = subprocess.run(["redis-cli", "ping"], capture_output=True, text=True)
    assert "NOAUTH" in res_no_auth.stdout or "NOAUTH" in res_no_auth.stderr or "Authentication required" in res_no_auth.stdout or "Authentication required" in res_no_auth.stderr, "Security violation: Redis can be accessed without a password!"

    res_with_auth = run_redis_cmd("ping")
    assert "PONG" in res_with_auth.stdout, "Redis server is not running or auth failed with the correct password."

def test_02_supervisor_active():
    """Verify that the consumer service is managed by supervisor and is running."""
    assert "RUNNING" in subprocess.run(["supervisorctl", "status", "consumer"], capture_output=True, text=True).stdout

def test_03_health_check_port_8080():
    """Verify the dynamic health check responds with 200 OK when healthy."""
    req = urllib.request.urlopen("http://localhost:8080", timeout=2)
    assert req.read().decode().strip() == "OK"

def test_04_security_and_privileges():
    """Verify all privilege separation and security constraints from the prompt."""
    res_user = subprocess.run(["id", "-u", "mq-worker"], capture_output=True, text=True)
    assert res_user.returncode == 0, "Requirement failed: System user 'mq-worker' does not exist."

    res_proc = subprocess.run(["pgrep", "-u", "mq-worker", "-f", "consumer.py"], capture_output=True)
    assert res_proc.returncode == 0, "Requirement failed: The consumer process is NOT running as the 'mq-worker' user."

    secret_file = Path("/etc/redis_secret")
    assert secret_file.exists(), "/etc/redis_secret does not exist."
    stat_res = subprocess.run(["stat", "-c", "%a:%U:%G", "/etc/redis_secret"], capture_output=True, text=True)
    assert stat_res.stdout.strip() == "400:root:root", f"Security violation! /etc/redis_secret permissions/owner should be strictly '400:root:root', but got '{stat_res.stdout.strip()}'"

def test_05_normal_processing():
    """Verify normal task processing."""
    if not LOG_FILE.exists():
        LOG_FILE.touch()
        subprocess.run(["chown", "mq-worker:mq-worker", str(LOG_FILE)])
    LOG_FILE.write_text("")
    run_redis_cmd("rpush", "task_queue", "NormalTask_1")
    time.sleep(2)
    assert "NormalTask_1" in LOG_FILE.read_text()

def test_06_crash_recovery():
    """Verify the auto-restart capability via Supervisor."""
    run_redis_cmd("rpush", "task_queue", "CRASH")
    time.sleep(4)
    assert "RUNNING" in subprocess.run(["supervisorctl", "status", "consumer"], capture_output=True, text=True).stdout
    run_redis_cmd("rpush", "task_queue", "Task_AfterCrash")
    time.sleep(2)
    assert "Task_AfterCrash" in LOG_FILE.read_text()

def test_07_redis_downtime_resilience():
    """Chaos Engineering: Verify system survives Redis outages and reflects status dynamically."""
    subprocess.run(["killall", "-9", "redis-server"])
    subprocess.run(["rm", "-f", "/var/run/redis/redis-server.pid"])
    time.sleep(2)

    try:
        urllib.request.urlopen("http://localhost:8080", timeout=2)
        assert False, "Health check should have failed with 503."
    except urllib.error.HTTPError as e:
        assert e.code == 503

    subprocess.run(["service", "redis-server", "start"])
    time.sleep(3)

    req = urllib.request.urlopen("http://localhost:8080", timeout=2)
    assert req.read().decode().strip() == "OK"

    run_redis_cmd("rpush", "task_queue", "Task_AfterRedisRestart")
    time.sleep(2)
    assert "Task_AfterRedisRestart" in LOG_FILE.read_text()

def test_08_logrotate_resilience():
    """Chaos Engineering: Verify system survives external log file rotation (FileNotFound & Permission stripping)."""

    # === 场景 1: 删除真实的日志文件，测试自动重建 ===
    subprocess.run(["rm", "-f", str(LOG_FILE)])
    run_redis_cmd("rpush", "task_queue", "Task_FileNotFound")
    time.sleep(2)

    assert "RUNNING" in subprocess.run(["supervisorctl", "status", "consumer"], capture_output=True, text=True).stdout, "Consumer crashed when encountering FileNotFoundError!"
    content = LOG_FILE.read_text() if LOG_FILE.exists() else ""
    assert "Task_FileNotFound" in content, "Failed to recover and write logs when log file was deleted."

    # === 场景 2: 权限剥夺与重试 (Graceful Retry) 测试 ===
    subprocess.run(["touch", str(LOG_FILE)])
    subprocess.run(["chown", "root:root", str(LOG_FILE)])
    subprocess.run(["chmod", "600", str(LOG_FILE)])

    run_redis_cmd("rpush", "task_queue", "Task_PermissionError")
    time.sleep(2)

    assert "RUNNING" in subprocess.run(["supervisorctl", "status", "consumer"], capture_output=True, text=True).stdout, "Consumer crashed when encountering PermissionError!"

    # 恢复权限，让挂起的任务得以重试写入
    subprocess.run(["chown", "mq-worker:mq-worker", str(LOG_FILE)])
    subprocess.run(["chmod", "666", str(LOG_FILE)])
    time.sleep(3)

    content = LOG_FILE.read_text()
    assert "Task_PermissionError" in content, "Task was lost! The consumer did not gracefully retry logging the task after PermissionError."
