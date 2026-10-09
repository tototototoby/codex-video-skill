# codex-video-skill

一个本地优先的长视频证据审阅技能。它将视频整理成带时间信息的抽帧、可选片段、字幕或本地转写，并生成审阅报告与覆盖范围文件。它帮助回答“哪些证据实际看过”，不会把少量抽样说成完整连续审阅。

媒体路径支持常用自包含视频容器（MP4、MKV/WebM、AVI、TS、OGG、FLV、WMV/ASF、NUT），具体编码还需解码库支持。不直接接收 URL、HLS 或 concat 播放列表，以免媒体引用绕到网络或其他本地文件。网页视频通过获授权工具导出文件或截图后处理。

## 安装

将整个 `codex-video-skill` 文件夹放入 Codex 用户技能目录。官方文档推荐 `~/.agents/skills`；Codex 也会读取项目中的 `.agents/skills`。Windows 常见位置：

```text
%USERPROFILE%\.agents\skills\codex-video-skill
```

从公开仓库安装（目标目录已有文件时先检查，不覆盖现有技能）：

```powershell
git clone https://github.com/tototototoby/codex-video-skill.git "$env:USERPROFILE\.agents\skills\codex-video-skill"
Set-Location "$env:USERPROFILE\.agents\skills\codex-video-skill"
```

```bash
git clone https://github.com/tototototoby/codex-video-skill.git ~/.agents/skills/codex-video-skill
cd ~/.agents/skills/codex-video-skill
```

目录说明见 [Codex Skills 文档](https://developers.openai.com/codex/skills)。

在技能目录创建并安装本地 Python 环境：

本项目需要 Python 3.10 或更新版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

macOS/Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

若要使用 `--transcribe`，安装 ASR 可选依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[asr]"
```

```bash
.venv/bin/python -m pip install -e ".[asr]"
```

使用本技能自带的 Python 运行 `scripts/video.py`，也可调用安装后生成的 `codex-video` 命令。如 Codex 尚未发现刚安装的技能，重启 Codex 后再试。

## 快速流程

在技能目录中运行命令。先准备视频，再查看输出中的抽帧与字幕；确认需要另外的模型服务且用户已同意后，才运行带 `--allow-upload` 的 `analyze`。提取在本机完成；将图片交给当前 Codex 会话查看时，会由该会话所用模型处理，因此这一步并不承诺离线运行。

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py .\scripts\video.py prepare "C:\path\to\video.mp4" --out .\work\review --segment-seconds 60 --sample-seconds 10 --subtitles .\captions.srt
& $py .\scripts\video.py report .\work\review
```

有外部字幕时使用 `--subtitles`；只在确实需要本地语音识别时加 `--transcribe`。同一命令和输入指纹可用于恢复准备任务。更多选项请看 `python scripts/video.py --help`。

`examples/make_demo.py --output work/demo.mp4` 可生成用于试跑的合成演示视频和同目录的 `work/demo.srt`。演示画面与匹配字幕是合成的，音轨只有合成提示音，没有人声，不代表真实产品行为。生成演示素材后可用以下命令创建报告：

```powershell
& $py .\examples\make_demo.py --output .\work\demo.mp4
& $py .\scripts\video.py prepare .\work\demo.mp4 --out .\work\demo-run --segment-seconds 4 --sample-seconds 2 --subtitles .\work\demo.srt
& $py .\scripts\video.py report .\work\demo-run
```

## 审阅方式

- **本地证据**：`prepare` 生成抽帧、可选剪辑与索引。Codex 实际查看帧后，用 `review` 写入观察和已查看帧 ID。
- **本地 ASR**：仅在显式 `--transcribe` 时处理视频音轨。首次运行可能下载所选模型；`--device` 指定 `cpu` 或 `cuda`。同一准备任务不能同时使用外部字幕与 ASR。
- **云端模型**：`analyze` 必须明确指定服务；`--allow-upload` 表示允许提交该适配器需要的素材。OpenAI 路径发送 PNG 图片与字幕文本，不发送原始音频。Gemini 是实际视频上传适配器，需要先用 `prepare --native-clips` 生成 MP4 片段；其视频/音频处理范围见 [providers.md](references/providers.md)。
- **浏览器捕获**：`browser-import` 只导入当前获授权浏览器工具导出的截图和字幕记录，不会启动浏览器或读取其配置。格式见 [browser.md](references/browser.md)。
- **报告**：`report` 生成 `report.md` 和 `coverage.json`。`mock` 只验证流程，不能提供语义结论。

## 证据边界

报告应分别说明：已准备的视频区间、成功提交模型并取得有效结果的分段、抽帧的单点时间、字幕跨度、本地 ASR 实际转写的音频范围，以及 Codex 真正查看的帧。抽帧无法证明两帧之间的变化；字幕不能代替原始音频；预录画面不能证明实时行为；也无法核验供应商内部实际处理了片段中的哪些帧或音频。结论按证据标记为 `spoken_claim`、`slide`、`prerecorded`、`simulated_data`、`visible_app_behavior` 或 `uncertain`。详见 [evidence.md](references/evidence.md)。

## 参考资料

- [SKILL.md](SKILL.md)：Codex 调用流程
- [browser.md](references/browser.md)：浏览器捕获导入格式
- [evidence.md](references/evidence.md)：证据、结论与覆盖范围
- [providers.md](references/providers.md)：模型数据流与上传控制
- [ATTRIBUTION.md](ATTRIBUTION.md)：来源与许可证说明
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)：依赖的独立许可证
- [VALIDATION.md](VALIDATION.md)：已运行的验证与未验证范围

## 模型配置示例

先在本机安全设置密钥环境变量，不打印其值。将示例模型名替换为你所选服务中实际支持相应输入的模型。`--base-url` 是 API 根路径，例如 OpenAI 兼容服务的 `/v1`；不要在 URL 中放密钥、查询参数或用户名密码。

```text
codex-video analyze RUN --provider openai --model YOUR_IMAGE_MODEL --base-url https://api.openai.com/v1 --key-env VIDEO_API_KEY --allow-upload
codex-video analyze RUN --provider gemini --model YOUR_VIDEO_MODEL --key-env VIDEO_API_KEY --allow-upload
```

Gemini 默认根路径为 `https://generativelanguage.googleapis.com/v1beta`，片段超过 20 MiB 时会拒绝提交，需重新以更短分段准备。当前适配器使用有大小上限的内联视频，不会自动上传到文件存储。OpenAI 兼容服务还需支持 Chat Completions 图像输入及 `response_format=json_object`；不支持时明确失败，不降级为纯文本。

## 续跑与深入核查

重复同一 `prepare` / `analyze` 命令会校验证据并跳过完整保存的结果。更改源文件、字幕、采样参数或已完成分析的模型/问题时，应使用新运行目录；显式 `review` 可替换某段的当前分析结果。运行目录只保存每段当前结果，不是不可修改的历史审计库。

进程正常退出会释放锁；强行结束后可能保留 `.run.lock`，只有确认没有进程仍在使用该目录时才手动移除。未保存结果的远端请求可能已经执行，重试可能再次收费；本工具只保证跳过已完整持久化的结果，无法提供服务端恰好一次语义。

针对遗漏或需要核查的时点，可独立提取更多证据：

```text
codex-video frame LOCAL --at 123.4 --out work/followup.png
```

查看返回的 `requested_time` 和 `actual_time`，将这张图与原报告一起核查；该命令不改写原运行记录。
