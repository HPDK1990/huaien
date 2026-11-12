# 怀恩 · WSL 弹窗抓现行 (WMI 进程启动事件版)
#
# 为什么用 WMI 而不是轮询 psutil:
#   wsl.exe 在这台机器上 (没装 WSL 可选组件) 会 **0.4 秒内退出** ——
#   轮询很容易错过。Win32_ProcessStartTrace 是**事件驱动**, 进程一起来就抓到。
#
# 为什么用 Wait-Event 而不是 -Action:
#   -Action 块跑在独立运行空间, $Event.SourceEventArgs 经常取不到,
#   出错还被静默吞掉 (表现为"监听启动了但什么都没抓到")。
#   Wait-Event 在主循环里处理, 作用域正常, 报错看得见。
#
# 编码: 必须是 **UTF-8 with BOM**。Windows PowerShell 5.1 按 ANSI 读无 BOM 的文件,
#   中文字节会被当成语法字符 -> "Unexpected token '}'"。写入用 encoding="utf-8-sig"。

$ErrorActionPreference = 'Continue'
$LogFile = Join-Path $PSScriptRoot '..\logs\wsl_watch.log'
$SourceId = 'HuaienWslWatch'

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
}

Unregister-Event -SourceIdentifier $SourceId -ErrorAction SilentlyContinue

try {
    Register-CimIndicationEvent -Query "SELECT * FROM Win32_ProcessStartTrace" -SourceIdentifier $SourceId -ErrorAction Stop
} catch {
    Write-Log ("订阅失败: {0}" -f $_.Exception.Message)
    exit 1
}

Write-Log "开始监听进程启动 (只记 wsl 相关)。Ctrl-C 停。"

try {
    while ($true) {
        $evt = Wait-Event -SourceIdentifier $SourceId -Timeout 1
        if ($null -eq $evt) { continue }
        try {
            $p = $evt.SourceEventArgs.NewEvent
            $name = [string]$p.ProcessName
            if ($name -match 'wsl') {
                $ppid = $p.ParentProcessID
                $pname = '?'
                $pcmd = ''
                try {
                    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ppid" -ErrorAction SilentlyContinue
                    if ($parent) {
                        $pname = $parent.Name
                        $pcmd = $parent.CommandLine
                    }
                } catch {}
                Write-Log ("*** 发现 {0} | pid={1} ppid={2}" -f $name, $p.ProcessID, $ppid)
                Write-Log ("    父进程: {0}" -f $pname)
                if ($pcmd) { Write-Log ("    父进程命令行: {0}" -f $pcmd) }
            }
        } catch {
            Write-Log ("处理事件出错: {0}" -f $_.Exception.Message)
        } finally {
            Remove-Event -EventIdentifier $evt.EventIdentifier -ErrorAction SilentlyContinue
        }
    }
} finally {
    Unregister-Event -SourceIdentifier $SourceId -ErrorAction SilentlyContinue
    Write-Log "停了。"
}
