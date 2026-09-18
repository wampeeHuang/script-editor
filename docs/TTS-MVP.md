# 本机预览与豆包生成 MVP

2026-09-16，真源：当前工具架manifest、本机代码/已安装依赖、当前工程API与浏览器。

决策：previewBackend=cosyvoice，defaultBackend=doubao。预览临时音频不回写成品index，不把旧稿音频标成新稿成品。正文未修改，豆包未批量调用。

## 模型比较与范围
CosyVoice3：官方0.5B模型；中文、方言、音色克隆和指令控制能力适合中文文案预览。现有0.5B基础模型，未切换RL权重。Confucius4-TTS：官方强调14语种、无需参考文本的跨语种克隆，现有本机项目明确sm_120推理不可用，走有道官方远程Gradio，不能算离线默认。
两者没有本次同参考音色的盲听对比，不宣称绝对音质最好。选择CosyVoice是结合用途和实际本机可运行性的工程判断。专有词、多音字与音色听感仍须人工验收。
来源：https://github.com/QwenAudio/CosyVoice 、https://github.com/netease-youdao/Confucius4-TTS 。

## 本机兼容与真实验证
原WebUI已有用户未提交改动，保留。独立cosyvoice_server.py提供TorchAudio info兼容、ONNX前端CPU（现有ORT需要未安装CUDA13 DLL）、文件式输出与CV3提示结束标记去重。主模型GPU，当前禁用flash/memory-efficient SDPA及TF32。
初始共享Transformers4.57.3生成内容不符；仅修提示、切CPU或math SDP仍失败。隔离目录_runtime/cosyvoice-deps固定Transformers4.51.3及tokenizers0.21.4后通过；共享Python环境未修改。恢复启动入口已同步工具架cosyvoice3 manifest。
短句：掼蛋小白们，快来吧！→1.92秒；Whisper base CPU回读“慣淡小白漫快来吧”。
长句：我做了一个掼蛋小白小指南网页，挂在这篇笔记下方，点开就能看。→6.48秒；回读“我做了一个关大小白小纸南网页挂在这篇笔记下方点开就能看”。回读支持总体内容一致，专有词误差无法仅靠ASR区分。
有效样本：_runtime/audit-20260916/cosyvoice-pinned.mp3、cosyvoice-long.mp3。此前preview/math/cpu/correct样本为失败诊断证据，不作交付母带。
浏览器点击预览后生成并播放完成，游标从第二句2.2秒推进至8.7秒，预览仍沿用稿件估时轨道，不能作为正式字幕精确时间。正式生成使用真实成品音频时长。

## 豆包MVP
已有豆包小何接口，当前密钥就绪（不记录凭证）。当前工程14单元/7章节/477字；确认页明确付费且单价未设置，不显示免费。未启动批量合成、未投放成片；整片音频、字幕与视频输出尚未验收。
代码新增本机独立预览后端及真实错误提示；播放器按预览默认调用，本机暂存mp3。正式生成单独选择豆包。

## 恢复与边界
恢复副本位于_runtime/audit-20260916：before-local-tts.py、before-local-service.py、before-cosy-ui.html、before-tts-defaults-project.json、before-cosyvoice-manifest.json。
配置曾误用API返回audioBase作base参数，生成了真实工程narration/narration下的本轮副本；已更正为父目录base并验证权威project.json双默认。该副本保留，不作真源。
既有投放旧音频与历史恢复缺陷尚未处理。底部混合成品/本机预览连续播放已在下节完成；清单单元试听仍保留原入口，未在此次重做。

## 旧配置回写兼容修正
2026-09-16：用户旧页面把CosyVoice未启用command占位配置回写权威工程，导致试听再次失败。normalize_project识别完整旧模板（id、kind、enabled、script、command全匹配），迁移至本机HTTP后端并恢复双默认。自定义script/command、新版明确禁用配置不迁移。真实旧payload POST预览通过1.92秒，权威工程规范化保存通过。避免仅修改磁盘配置而继续被旧客户端覆盖。恢复源码before-legacy-tts-migration.py。

## 底部连续流式播放与信息精简（2026-09-16）
本机CosyVoice以stream=True产生PCM片段，经两个本地HTTP接口实时转发；浏览器AudioContext边收边排队播放。一次点击从当前句起连续跨单元、跨章节，已有有效成品直接播放缓存，dirty单元调用免费本机流式预览。限制排队约8秒，支持暂停/继续，停止、切换位置、编辑和载入工程取消请求及旧回调；章节/单元间隔按工程配置保留。不会自动调用付费后端，也不回写临时预览为成品。
移除独立“本机预览”文字，来源收进播放按钮提示；时间缩小放入全宽轨道右下角。时间轨道仍以稿件估时映射，正式成品字幕不能用该预览游标作为精确时间。
真实接口长句3个PCM片段：首片4.343秒到达，末片6.406秒，首片早于合成完成；冷启动和算力不足仍可能等待，未承诺无间隙实时率。证据stream-events.json、stream-long.wav。
真实浏览器：一次播放跨多个单元；暂停后时间00:09.3保持超过10秒，继续推进。隔离混合工程成品第一句+本机第二章两句全部播放，最终第3句、00:07.5/00:07.5、idle；最终版本含配置间隔复测通过。真实稿件未修改，豆包未调用。
恢复点：before-stream-server.py、before-continuous-player.html，均在_runtime/audit-20260916。

## 逐行预览同步修正（2026-09-17）
底部本机预览改为每个可见非空行独立合成并排队；同单元多行以sentenceOnly请求只生成指定行。每行记录PCM片段实际排程区间，高亮仅在该行音频出声时亮起，缓冲断档不累加为已播放时长。底部预览当前全部使用本机逐行音频，不再混合整单元成品缓存；正式合成仍按句末标点分单元，清单成品试听保留。此决策优先保证预览行与声音对应，逐行语气可能比整段合成稍碎，轨道数值仍以稿件估时映射。
验证：JavaScript语法与Python编译通过；同单元[1,2]实际接口逐行返回0.84秒、3.08秒音频；免费本机调用，无工程写入。浏览器控制连接失败，本轮尚未完成页面听音对照验收。恢复副本before-row-playback-20260917.html、before-row-service-20260917.py。

## 当前决策：双路径与真实音频轨道（2026-09-17，取代前述本机TTS预览方案）
用户确认仅保留浏览器本机朗读和豆包API。默认previewBackend=browser、defaultBackend=doubao；退役IndexTTS/CosyVoice及其他配置保存在meta.retiredBackends，不删除模型或音频。卡片自定义后端退役选择保存在retiredBackend。新默认注册表只有两项，后端界面只展示两项。本机朗读不作为正式生成选项，按原整句合成单元全文朗读，不逐行拆碎；依赖浏览器真实声音能力，未宣称零延迟或保证可用。
底部audioTimeline独立于稿件compute_timeline，仅包含clean且有音频、正时长的单元，真实duration累加，不添加稿件估时或未实际生成的配置间隔。尚无有效音频时显示尚无成品音频并禁用播放；有缺失配音时明确标记部分配音。播放以解码音频实际排程进度推进，点击轨道可在单元内按真实秒数定位；整单元高亮，不假定精确行边界，不改变编辑选中行。此轨道表示当前有效音频串联，与尚未验收的投放合并输出不混为一谈。
主工程浏览器验证：14个dirty，底部无成品音频，两个后端，Cosy/Index占位消失。隔离工程两段已有音频各1.92秒，dirty文案排除，总3.84秒；浏览器一次播放正常结束3.8/3.8，轨道60%点击从2.3秒播放，暂停状态正常。JS语法、Python编译通过，无付费生成。浏览器控制已恢复。本轮停止此前本任务启动的CosyVoice服务，不影响本机朗读或豆包路径。
稿件卡片/行内估时仍保留，按用户要求后续调整表达。正式字幕单元内部仍为字数分配，不能宣称句级真实对齐。恢复点before-real-track-{model.py,tts.py,文案脚本工作台.html}，位于_runtime/audit-20260916。
