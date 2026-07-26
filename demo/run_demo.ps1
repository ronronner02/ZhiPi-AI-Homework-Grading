# 智批π Demo 一键启动（PowerShell）
# 自动探测可用 Python（避开 Microsoft Store 占位命令）→ 安装依赖 → 启动服务 → 打开浏览器
Set-Location $PSScriptRoot

# 候选解释器：优先 py launcher（-3），回退 python；逐一探测能否真正执行
$py = $null
foreach ($cand in @(@("py", "-3"), @("python"))) {
    $exe = $cand[0]
    $exeArgs = @($cand | Select-Object -Skip 1)   # 单元素候选得到空数组
    try {
        & $exe @($exeArgs + @("-c", "import sys")) 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host "[错误] 未找到可用的 Python（3.10+），请从 https://www.python.org 安装后重试。" -ForegroundColor Red
    exit 1
}

$exe = $py[0]
$exeArgs = @($py | Select-Object -Skip 1)
Write-Host "[智批π] 使用解释器：$($py -join ' ')"
& $exe @($exeArgs + @("-m", "pip", "install", "-r", "requirements.txt", "-q"))
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 依赖安装失败，请手动执行：$($py -join ' ') -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

Write-Host "[智批π] 启动服务：http://127.0.0.1:8010 （Ctrl+C 停止）"
Start-Process "http://127.0.0.1:8010"
& $exe @($exeArgs + @("-m", "uvicorn", "app:app", "--port", "8010"))
