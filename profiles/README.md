# profiles/ 目录 —— profile 文件的模板与备份

这里放的是 **模板**。真正生效的文件在:

```
%LOCALAPPDATA%\AppData\Local\hermes\profiles\huaien\
├── SOUL.md      ← 人格契约 (本目录 huaien-SOUL.md 的部署目标)
├── POLICY.md    ← 主动行动规则 (完整版, 人读)
└── config.yaml  ← 工具集/路由配置
```

## 为什么要放一份在项目里

profile 目录**不在 git 里** —— 换个机器、重装 Hermes, 人格契约就丢了。
放一份模板在这里, 有版本历史、能 diff、改完能重新部署。

## 部署

```powershell
Copy-Item "<PROJECT_ROOT>\profiles\huaien-SOUL.md" `
          "$env:LOCALAPPDATA\hermes\profiles\huaien\SOUL.md" -Force
```

改人格契约的流程: 改本目录的模板 -> 部署过去 -> 重启 gateway(或等新会话生效)。

## 当前版本要点 (2026-11-04)

主怀恩改成**路由层**: 只做评估/派工/回复, 重活一律派给 worker。
配套硬约束: `platform_toolsets.weixin` / `.feishu` 只留 9 个轻工具,
**没有 terminal / file / code_execution** —— 物理上没法自己蹲着干活。
详见 `docs/方案-主怀恩路由层改造.md`。
