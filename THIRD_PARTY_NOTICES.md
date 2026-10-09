# 第三方依赖

本仓库不包含上游 VideoAgent 源码、第三方二进制、模型权重或用户视频。依赖由用户通过 Python 包管理器安装，其许可证与本仓库 MIT 许可证分别适用。

| 依赖 | 项目与许可来源 |
| --- | --- |
| PyAV | https://github.com/PyAV-Org/PyAV （BSD-3-Clause；其 FFmpeg 依赖另有许可） |
| Pillow | https://github.com/python-pillow/Pillow （HPND 类许可，见 LICENSE） |
| imageio-ffmpeg | https://github.com/imageio/imageio-ffmpeg （BSD-2-Clause；wheel 内 FFmpeg 二进制另有许可） |
| faster-whisper（可选） | https://github.com/SYSTRAN/faster-whisper （MIT；CTranslate2 与模型权重另有许可） |
| FFmpeg | https://ffmpeg.org/legal.html （具体构建可能适用 LGPL 或 GPL） |

FFmpeg 的具体编译选项可用其 `-version` 输出检查。该工具通过独立 FFmpeg 进程生成媒体；本仓库的发布包不捆绑该程序。如重新打包或分发依赖二进制，请同时核对对应版本的许可证、源码提供要求和模型许可。
