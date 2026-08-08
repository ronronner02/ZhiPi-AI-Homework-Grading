"""静态前端交付前审计：禁用表情符号、禁止任何外部资源请求。

比赛现场有断网预案，任何 CDN 引用都会让页面在断网时降级；
表情符号则是明确的设计禁令。两条都属于「一旦破线就必须立刻发现」，
所以做成可重复执行的脚本，而不是靠人眼过一遍。

用法：py -3 tools/audit_static.py [静态目录]
退出码 0 表示干净，1 表示有问题。
"""
import pathlib
import re
import sys

EMOJI = re.compile(
    '['
    '\U0001F000-\U0001FAFF'   # 各类符号与象形文字
    '\U00002600-\U000027BF'   # 杂项符号与装饰符
    '\U0001F1E6-\U0001F1FF'   # 区域指示符（旗帜）
    '\U00002B00-\U00002BFF'   # 杂项符号与箭头
    '️'                  # 变体选择符-16（表情呈现）
    ']'
)

# 只认真正会发起请求的写法。SVG 命名空间 URI 与注释里的授权链接不算。
EXTERNAL = re.compile(
    r'(?:src|href)\s*=\s*["\']\s*(?:https?:)?//'
    r'|@import\s+(?:url\()?\s*["\']?\s*(?:https?:)?//'
    r'|url\(\s*["\']?\s*(?:https?:)?//',
    re.I,
)

ALLOW_SUBSTR = (
    'http://www.w3.org/2000/svg',   # SVG 命名空间，不产生网络请求
    'https://lucide.dev',           # 图标授权声明，仅出现在注释里
)


def scan(root: pathlib.Path) -> int:
    issues = 0
    files = 0
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        files += 1
        text = path.read_text(encoding='utf-8', errors='replace')
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in EMOJI.finditer(line):
                print(f'EMOJI    {path}:{lineno}  {m.group()!r}')
                issues += 1
            for m in EXTERNAL.finditer(line):
                if any(a in line for a in ALLOW_SUBSTR):
                    continue
                print(f'EXTERNAL {path}:{lineno}  {line.strip()[:100]}')
                issues += 1
    print(f'扫描 {files} 个文件，发现 {issues} 处问题')
    return issues


if __name__ == '__main__':
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else 'static')
    if not target.exists():
        print(f'目录不存在：{target}')
        raise SystemExit(2)
    raise SystemExit(1 if scan(target) else 0)
