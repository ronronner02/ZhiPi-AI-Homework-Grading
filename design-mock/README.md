# 智批π UI 预览

静态单页预览（**不修改** `demo/static/**`）。

## 打开

双击 `index.html`，或：

```powershell
npx --yes serve .
```

## 交互（单页 Tab）

| 视图 | 怎么进 |
|------|--------|
| 首页 | 默认；点品牌 / 重置 |
| 工作台 | 首页「开始批改」或口令「进入」 |
| 01–04 | 顶栏 Tab，一次只显示一屏 |
| 跨屏 | 提交批改 / 去教师台 / 飞书 CTA 等 |

预览内可点：文件夹选中、教师台表格筛选/确认/终审弹层、飞书推送与同步、讲评大纲生成/复制。

教师台对齐现网：筛选条 + 表格列表 + 终审弹窗（非 master–detail）。

Hash：`#hero` `#submit` `#result` `#teacher` `#board` `#gate`。

## 文件

- `index.html` — 结构与各屏内容
- `styles.css` — 视觉 token 与布局
- `app.js` — 视图 / Tab 切换

设计依据：`../DESIGN.md`。
