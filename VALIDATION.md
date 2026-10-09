# v0.1.0 验证范围

验证日期：2026-10-09。实际本机环境为 Windows / Python 3.12；持续集成另在 Linux、Windows 与 Python 3.10、3.12 上运行，状态以 GitHub Actions 为准。

## 已实际完成

- 从全新隔离环境安装核心依赖及项目 CLI；标准 `SKILL.md` 通过 skill-creator 的 frontmatter 校验。
- 自动测试真实合成视频解码、CFR/VFR 采样时间、音画起始偏移、视频音轨/MP4 片段导出、输入/输出边界。
- 本机 HTTP 测试服务捕获 OpenAI 兼容请求中的 PNG 字节与字幕、Gemini 请求中的 MP4 字节。校验上传开关、重定向拒绝、错误脱敏、字节哈希与引用时间。
- 分段中断后续跑，完整保存的结果无重复；源文件/参数变化与证据篡改被拒绝；缺失/失败区间单独记录，Mock 不计入语义结果。
- GPT-6 独立试用：实际打开合成视频的 0、2、4、6、8、10 秒六张截图，生成三段带引用的审阅结果，未把生成卡片、示例字幕当作真实产品行为或实际口述。
- 报告转义不可信 Markdown/HTML；浏览器导入仅存来源站点，保留实际播放时间、请求时间及偏差，并检查截图来源与输出哈希。

## 尚未声称完成的验证

- 没有配置额外模型 API 凭据，未进行 OpenAI/Gemini 真实服务端语义调用。上面的 HTTP 测试只证明客户端确实发送媒体字节；Codex 当前会话查看截图是另一条实际运行路径。
- 未对真实人声进行 Whisper 转写质量验收；演示音轨是合成提示音，SRT 明确标记为非转录。ASR 是可选本地路径，首次模型下载、CUDA 环境和识别准确率应在使用环境验证。
- 浏览器导入已测试格式、时间偏差、缺失区间和隐私边界；尚未通过实际第三方播放器完整采集。导入程序无法验证外部填写的播放时间，见 [browser.md](references/browser.md) 的人工验收步骤。
- 这些合成小样和测试不是小时级吞吐量、内存上限或所有媒体编码格式的压力验收。

提取在本机完成。Codex 看图时由当前会话模型处理；这不等于完全离线。原生视频字节提交和离散截图查看都不能被表述为每秒、每帧的连续覆盖。

开发验证命令：

```text
python -m pip install -e ".[dev]"
python -m pytest -q
python examples/make_demo.py --output work/demo.mp4
codex-video prepare work/demo.mp4 --out work/demo-run --segment-seconds 4 --sample-seconds 2 --subtitles work/demo.srt --audio --native-clips --max-segments 1
codex-video prepare work/demo.mp4 --out work/demo-run --segment-seconds 4 --sample-seconds 2 --subtitles work/demo.srt --audio --native-clips
```
