@echo off
rem ============================================================
rem  push-all.bat —— 把 pan-organizer 同时推送到三个远程仓库
rem
rem  用法：
rem    push-all.bat          推送当前分支到 gitcode / gitee / github
rem    push-all.bat setup    只配置三个远程，不推送
rem
rem  日志：
rem    push-logs\push-<时间戳>.log   本次完整日志（git 原始输出 + 成败结论）
rem    push-logs\history.log         历次推送一行摘要（追加式）
rem
rem  说明：
rem    - gitcode 走 SSH；gitee / github 走 HTTPS（首次推送弹凭据窗口，输一次就记住）
rem    - 脚本幂等：重复运行没副作用，远程地址与脚本不一致时自动纠正
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem    - gitcode 走 SSH（该平台已禁用密码认证，HTTPS 必须用私人令牌）
set "GITCODE_URL=git@gitcode.com:SimianLee/pan-organizer.git"
rem    - gitee / github 走 HTTPS：首次推送会弹凭据窗口，输一次就会记住
set "GITEE_URL=https://gitee.com/SimianLee/pan-organizer.git"
set "GITHUB_URL=https://github.com/SimianLee/pan-organizer.git"

rem ---------- 0) 准备日志目录与时间戳 ----------
if not exist "push-logs" mkdir "push-logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "NOW=%%i"
set "PLOG=push-logs\push-%STAMP%.log"
set "HIST=push-logs\history.log"
set "TMPR=push-logs\.tmp-last.log"

rem ---------- 1) 幂等配置三个远程 ----------
rem origin 如果指向 gitee，统一改名为 gitee，让三个远程名固定为 gitcode/gitee/github
git remote get-url gitee >nul 2>&1
if errorlevel 1 (
    git remote get-url origin 2>nul | findstr /i "gitee.com" >nul 2>&1
    if not errorlevel 1 git remote rename origin gitee >nul 2>&1
)
git remote get-url gitee   >nul 2>&1 || git remote add gitee   "%GITEE_URL%"
git remote get-url gitcode >nul 2>&1 || git remote add gitcode "%GITCODE_URL%"
git remote get-url github  >nul 2>&1 || git remote add github  "%GITHUB_URL%"
rem 远程已存在但地址与脚本不一致时，自动纠正（幂等）
git remote set-url gitee   "%GITEE_URL%"   2>nul
git remote set-url gitcode "%GITCODE_URL%" 2>nul
git remote set-url github  "%GITHUB_URL%"  2>nul

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
for /f "delims=" %%c in ('git rev-parse --short HEAD') do set "COMMIT=%%c"

rem ---------- 2) 写日志头 ----------
>>"%PLOG%" echo ============================================
>>"%PLOG%" echo  pan-organizer 三库推送日志
>>"%PLOG%" echo  时间: %NOW%
>>"%PLOG%" echo  分支: %BRANCH%    提交: %COMMIT%
>>"%PLOG%" echo ============================================

echo.
echo 当前分支: %BRANCH%   提交: %COMMIT%
echo 本次日志: %PLOG%
echo 远程列表:
git remote -v
echo.

if /i "%~1"=="setup" (
    echo [setup] 三个远程已就绪，未执行推送。
    goto :end
)

rem ---------- 3) 依次推送到三个远程 ----------
set /a FAIL=0
set /a OK=0
for %%r in (gitcode gitee github) do (
    for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "T0=%%i"
    echo ============================================
    echo  开始推送 %%r（分支 %BRANCH%）
    echo ============================================
    >>"%PLOG%" echo.
    >>"%PLOG%" echo ---- [%%r] 开始 %T0% ----
    git push -u %%r %BRANCH% > "!TMPR!" 2>&1
    set "RC=!errorlevel!"
    type "!TMPR!"
    type "!TMPR!" >> "%PLOG%"
    if !RC! equ 0 (
        echo  [成功] %%r 已推送。
        >>"%PLOG%" echo ---- [%%r] 结果: 成功 ----
        set "R_%%r=成功"
        set /a OK+=1
    ) else (
        echo  [失败] %%r 推送失败（exit=!RC!），详情见日志。
        >>"%PLOG%" echo ---- [%%r] 结果: 失败 exit=!RC! ----
        set "R_%%r=失败"
        set /a FAIL+=1
    )
    echo.
)
del "!TMPR!" >nul 2>&1

rem ---------- 4) 汇总 + 历史记录 ----------
>>"%PLOG%" echo ============================================
>>"%PLOG%" echo  汇总: 成功 %OK% / 失败 %FAIL%（gitcode=%R_gitcode% gitee=%R_gitee% github=%R_github%）
>>"%PLOG%" echo ============================================

>>"%HIST%" echo [%NOW%] 分支=%BRANCH% 提交=%COMMIT% 结果: gitcode=%R_gitcode% gitee=%R_gitee% github=%R_github%（详见 %PLOG%）

echo ============================================
if %FAIL% equ 0 (
    echo  全部完成：3 个仓库都推送成功。 ^(成功 %OK%^)
) else (
    echo  推送结束：成功 %OK%，失败 %FAIL%，见上方报错。
)
echo  完整日志: %PLOG%
echo  历史记录:
powershell -NoProfile -Command "Get-Content '%HIST%' -Tail 5"
echo ============================================

:end
endlocal & exit /b %FAIL%
