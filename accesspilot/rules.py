"""分流规则库: 面向 ChatGPT / Discord / X 等平台的定向策略.

设计取舍:
  * 关键平台的域名规则以 **内联** 形式写死(来自 blackmatrix7/ios_rule_script 与
    Loyalsoldier/clash-rules 的公开规则集), 保证即使规则集下载失败,
    目标平台仍然能正确分流 —— 这是"能正常使用"的底线。
  * 大体积的通用分类(Google/流媒体/GFW)走 rule-provider 远程订阅,
    避免配置臃肿, 并支持 jsDelivr 镜像以适配国内网络。
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# 策略组名称(与配置生成共用)
# --------------------------------------------------------------------------- #

G_SELECT = "🚀 节点选择"
G_AUTO = "♻️ 自动选择"
G_AI = "🤖 AI 服务"
G_SOCIAL = "💬 社交平台"
G_MEDIA = "📺 流媒体"
G_DIRECT = "🎯 全球直连"
G_REJECT = "🛑 广告拦截"
G_FINAL = "🐟 兜底分流"

#: 免节点直连加速(IP 优选)相关的组与节点名。
#: 这一组不放进 ALL_GROUPS —— 它只在开启直连加速时才出现在配置里。
G_ACCEL = "⚡ 直连加速"
ACCEL_PROXY_NAME = "⚡ IP优选"

ALL_GROUPS = [
    G_SELECT,
    G_AUTO,
    G_AI,
    G_SOCIAL,
    G_MEDIA,
    G_DIRECT,
    G_REJECT,
    G_FINAL,
]


def accel_rules() -> list[str]:
    """直连加速的域名规则(必须排在最前面, 优先于其它一切分流)."""
    from .accel import ACCEL_SUFFIXES

    seen: set[str] = set()
    out: list[str] = []
    for s in ACCEL_SUFFIXES:
        if s in seen:
            continue
        seen.add(s)
        out.append(f"DOMAIN-SUFFIX,{s},{G_ACCEL}")
    return out

# --------------------------------------------------------------------------- #
# 目标平台内联域名(可离线工作)
# 来源: blackmatrix7/ios_rule_script  Clash/OpenAI|Discord|Twitter|Telegram
# --------------------------------------------------------------------------- #

OPENAI_DOMAINS = [
    "openai.com",
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "openaiapi-site.azureedge.net",
    "openaicom.imgix.net",
    "ai.com",
    "sora.com",
    "chatgpt.livekit.cloud",
    "host.livekit.cloud",
    "turn.livekit.cloud",
    "auth0.com",
    "client-api.arkoselabs.com",
    "featuregates.org",
    "identrust.com",
    "intercom.io",
    "intercomcdn.com",
    "launchdarkly.com",
    "observeit.net",
    "segment.io",
    "sentry.io",
    "statsig.com",
    "statsigapi.net",
    "stripe.com",
    "algolia.net",
]

AI_EXTRA_DOMAINS = [
    "anthropic.com",
    "claude.ai",
    "claudeusercontent.com",
    "perplexity.ai",
    "poe.com",
    "midjourney.com",
    "x.ai",
    "grok.com",
    "generativelanguage.googleapis.com",
    "aistudio.google.com",
    "gemini.google.com",
    "bard.google.com",
    "copilot.microsoft.com",
    "bing.com",
    "cursor.com",
    "cursor.sh",
    "huggingface.co",
    "cohere.ai",
    "mistral.ai",
    "deepmind.com",
    "copilot-proxy.githubusercontent.com",
]

DISCORD_DOMAINS = [
    "discord.com",
    "discord.gg",
    "discord.co",
    "discord.design",
    "discord.dev",
    "discord.gift",
    "discord.gifts",
    "discord.media",
    "discord.new",
    "discord.store",
    "discord.tools",
    "discord-activities.com",
    "discordactivities.com",
    "discordapp.com",
    "discordapp.io",
    "discordapp.net",
    "discordcdn.com",
    "discordmerch.com",
    "discordpartygames.com",
    "discordsays.com",
    "discordstatus.com",
    "dis.gd",
    "airhorn.solutions",
    "airhornbot.com",
    "bigbeans.solutions",
    "hammerandchisel.ssl.zendesk.com",
    "watchanimeattheoffice.com",
]

X_DOMAINS = [
    "x.com",
    "twitter.com",
    "twitter.jp",
    "t.co",
    "twimg.com",
    "twimg.co",
    "twimg.org",
    "twttr.com",
    "twttr.net",
    "twtrdns.net",
    "twvid.com",
    "tweetdeck.com",
    "twittercommunity.com",
    "twitteroauth.com",
    "twitterstat.us",
    "twitterinc.com",
    "twitterflightschool.com",
    "ads-twitter.com",
    "periscope.tv",
    "pscp.tv",
    "vine.co",
    "twitpic.com",
    "cms-twdigitalassets.com",
    "tellapart.com",
    "twitter.biz",
]

TELEGRAM_DOMAINS = [
    "telegram.org",
    "telegram.me",
    "telegram.dog",
    "telegram.space",
    "telegram-cdn.org",
    "cdn-telegram.org",
    "t.me",
    "tx.me",
    "telegra.ph",
    "graph.org",
    "telesco.pe",
    "tdesktop.com",
    "telegramdownload.com",
    "contest.com",
    "comments.app",
    "legra.ph",
    "stel.com",
    "tg.dev",
    "usercontent.dev",
    "mbrx.app",
    "quiz.directory",
    "telega.one",
]

#: 国内常见被墙的通用站点(兜底内联, 防规则集下载失败)
GFW_FALLBACK_DOMAINS = [
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "googleusercontent.com",
    "googlevideo.com",
    "youtube.com",
    "youtu.be",
    "ytimg.com",
    "ggpht.com",
    "blogspot.com",
    "wikipedia.org",
    "wikimedia.org",
    "githubusercontent.com",
    "github.io",
    "ghcr.io",
    "npmjs.com",
    "docker.com",
    "docker.io",
    "medium.com",
    "reddit.com",
    "redd.it",
    "quora.com",
    "pinterest.com",
    "tumblr.com",
    "soundcloud.com",
    "spotify.com",
    "scdn.co",
    "netflix.com",
    "nflxvideo.net",
    "twitch.tv",
    "ttvnw.net",
    "themoviedb.org",
    "nytimes.com",
    "bbc.com",
    "bbc.co.uk",
    "wsj.com",
    "bloomberg.com",
    "reuters.com",
    "duckduckgo.com",
    "protonmail.com",
    "proton.me",
    "signal.org",
    "whatsapp.com",
    "whatsapp.net",
    "instagram.com",
    "cdninstagram.com",
    "facebook.com",
    "fbcdn.net",
    "messenger.com",
    "linkedin.com",
    "licdn.com",
    "microsoftonline.com",
    "live.com",
    "bing.com",
    "openai.com",
    "archive.org",
    "mozilla.org",
    "cloudflare.com",
    "cloudflare-dns.com",
    "1.1.1.1",
    "speedtest.net",
    "yt-dlp.org",
]


def _suffix_rules(domains: list[str], group: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for d in domains:
        d = d.strip().lower()
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(f"DOMAIN-SUFFIX,{d},{group}")
    return out


def inline_rules() -> list[str]:
    """生成内联规则(顺序即优先级)."""
    rules: list[str] = []
    rules += _suffix_rules(AI_EXTRA_DOMAINS, G_AI)
    rules += _suffix_rules(OPENAI_DOMAINS, G_AI)
    rules += _suffix_rules(DISCORD_DOMAINS, G_SOCIAL)
    rules += _suffix_rules(X_DOMAINS, G_SOCIAL)
    rules += _suffix_rules(TELEGRAM_DOMAINS, G_SOCIAL)
    rules += _suffix_rules(GFW_FALLBACK_DOMAINS, G_SELECT)
    return rules


# --------------------------------------------------------------------------- #
# rule-provider 目录
# --------------------------------------------------------------------------- #

#: jsDelivr 在国内通常可直连, 作为 GitHub 的首选镜像
JSdelivr_HOSTS = [
    "https://testingcf.jsdelivr.net",
    "https://fastly.jsdelivr.net",
    "https://cdn.jsdelivr.net",
]

GHPROXY_HOSTS = [
    "https://ghfast.top",
    "https://gh-proxy.com",
    "https://ghproxy.net",
]


def _loyalsoldier(path: str, mirror: str) -> str:
    return _gh_url("Loyalsoldier/clash-rules", "release", path, mirror)


def _blackmatrix7(category: str, mirror: str) -> str:
    return _gh_url(
        "blackmatrix7/ios_rule_script",
        "master",
        f"rule/Clash/{category}/{category}.yaml",
        mirror,
    )


def _gh_url(repo: str, ref: str, path: str, mirror: str) -> str:
    """构造 GitHub 资源 URL, 支持多种镜像策略."""
    if mirror == "raw":
        return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    if mirror == "ghproxy":
        return f"{GHPROXY_HOSTS[0]}/https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    return f"{JSdelivr_HOSTS[0]}/gh/{repo}@{ref}/{path}"


def rule_providers(mirror: str = "jsdelivr") -> dict[str, Any]:
    """返回 mihomo `rule-providers` 配置块.

    重要: Loyalsoldier/clash-rules 的文件虽然以 .txt 结尾, 内容却是 Clash
    YAML payload 格式(`payload:` + `- 'xxx'`)。若错标为 format: text,
    内核会把 'payload:' 当成规则逐行解析, 结果是**整份规则集被静默丢弃**。
    这里统一使用 yaml 格式, 并由单元测试锁定该约定。
    """

    def loy(name: str, behavior: str) -> dict[str, Any]:
        return {
            "type": "http",
            "behavior": behavior,
            "format": "yaml",
            "url": _loyalsoldier(f"{name}.txt", mirror),
            "path": f"./ruleset/{name}.yaml",
            "interval": 86400,
        }

    def bm7(cat: str) -> dict[str, Any]:
        return {
            "type": "http",
            "behavior": "classical",
            "format": "yaml",
            "url": _blackmatrix7(cat, mirror),
            "path": f"./ruleset/{cat}.yaml",
            "interval": 86400,
        }

    return {
        "reject": loy("reject", "domain"),
        "private": loy("private", "domain"),
        "direct": loy("direct", "domain"),
        "proxy": loy("proxy", "domain"),
        "gfw": loy("gfw", "domain"),
        "lancidr": loy("lancidr", "ipcidr"),
        "cncidr": loy("cncidr", "ipcidr"),
        "telegramcidr": loy("telegramcidr", "ipcidr"),
        "openai": bm7("OpenAI"),
        "discord": bm7("Discord"),
        "twitter": bm7("Twitter"),
        "youtube": bm7("YouTube"),
        "netflix": bm7("Netflix"),
        "spotify": bm7("Spotify"),
        "telegram": bm7("Telegram"),
        "google": bm7("Google"),
        "github": bm7("GitHub"),
        "microsoft": bm7("Microsoft"),
        "apple": bm7("Apple"),
        "category-ads-all": bm7("Advertising"),
    }


#: 规则集生效顺序(rule-provider 引用 -> 目标策略组)
def provider_rules() -> list[str]:
    return [
        f"RULE-SET,openai,{G_AI}",
        f"RULE-SET,discord,{G_SOCIAL}",
        f"RULE-SET,twitter,{G_SOCIAL}",
        f"RULE-SET,telegram,{G_SOCIAL}",
        f"RULE-SET,google,{G_SELECT}",
        f"RULE-SET,youtube,{G_MEDIA}",
        f"RULE-SET,netflix,{G_MEDIA}",
        f"RULE-SET,spotify,{G_MEDIA}",
        f"RULE-SET,github,{G_SELECT}",
        f"RULE-SET,microsoft,{G_SELECT}",
    ]


def tail_rules() -> list[str]:
    """规则尾部: 直连优先, 最后兜底."""
    return [
        f"RULE-SET,reject,{G_REJECT}",
        f"RULE-SET,category-ads-all,{G_REJECT}",
        f"RULE-SET,private,{G_DIRECT}",
        f"RULE-SET,lancidr,{G_DIRECT},no-resolve",
        f"RULE-SET,direct,{G_DIRECT}",
        f"RULE-SET,gfw,{G_SELECT}",
        f"RULE-SET,proxy,{G_SELECT}",
        f"RULE-SET,telegramcidr,{G_SOCIAL},no-resolve",
        f"RULE-SET,cncidr,{G_DIRECT},no-resolve",
        "GEOIP,CN," + G_DIRECT + ",no-resolve",
        f"MATCH,{G_FINAL}",
    ]


def build_rules(*, accel: bool = False) -> list[str]:
    return (accel_rules() if accel else []) + inline_rules() + provider_rules() + tail_rules()
