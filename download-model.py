#!/usr/bin/env python3
"""下载 faster-whisper 模型，绕过 DNS 污染"""

import socket
import os
import sys

# === DNS 修复: 将被污染的域名指向正确 IP ===
DNS_FIXES = {}

def resolve_via_doh(domain):
    """通过 Cloudflare DoH 解析域名"""
    import urllib.request
    import json
    try:
        url = f"https://1.1.1.1/dns-query?name={domain}&type=A"
        req = urllib.request.Request(url, headers={
            "accept": "application/dns-json"
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            for answer in data.get("Answer", []):
                if answer.get("type") == 1:
                    return answer["data"]
    except Exception as e:
        print(f"  DoH 解析 {domain} 失败: {e}")
    return None

def fix_dns():
    """修复被污染的域名解析"""
    domains_to_fix = [
        "huggingface.co",
        "cdn-lfs-us-1.huggingface.co",
        "cdn-lfs.huggingface.co",
    ]

    print("[DNS] 检测并修复域名解析...")
    orig_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, *args, **kwargs):
        if host in DNS_FIXES:
            ip = DNS_FIXES[host]
            return orig_getaddrinfo(ip, port, *args, **kwargs)
        return orig_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = patched_getaddrinfo

    # 动态解析并修复
    for domain in domains_to_fix:
        ip = resolve_via_doh(domain)
        if ip:
            DNS_FIXES[domain] = ip
            print(f"  {domain} -> {ip}")

    # 也拦截 xethub CDN (动态域名)
    orig_getaddrinfo_backup = socket.getaddrinfo
    def smart_getaddrinfo(host, port, *args, **kwargs):
        if host in DNS_FIXES:
            return orig_getaddrinfo_backup(DNS_FIXES[host], port, *args, **kwargs)
        if "huggingface" in host or "xethub" in host or "hf.co" in host:
            ip = resolve_via_doh(host)
            if ip:
                DNS_FIXES[host] = ip
                print(f"  {host} -> {ip} (动态解析)")
                return orig_getaddrinfo_backup(ip, port, *args, **kwargs)
        return orig_getaddrinfo_backup(host, port, *args, **kwargs)

    socket.getaddrinfo = smart_getaddrinfo
    print()

def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "large-v3"

    fix_dns()

    print(f"正在下载模型: {model}")
    print(f"仓库: Systran/faster-whisper-{model}")
    print()

    from faster_whisper import WhisperModel
    # 构造参数收敛到 engine 单一事实源(#16 CR 发现 4:不再硬编码 device='cuda',
    # VOICE_INPUT_ENGINE=cpu 的机器也能跑本脚本;auto 先试 cuda 失败降级 cpu)
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import engine
    eng = engine.engine_name(dict(os.environ))
    if eng == "npu":
        print("NPU engine not implemented yet — see issue #19", file=sys.stderr)
        sys.exit(1)
    kwargs = engine.construction_kwargs(eng)
    print("开始下载（首次约 3GB）...")
    print("进度条应该很快出现，如果没有说明网络仍有问题")
    print()

    try:
        model_obj = WhisperModel(model, **kwargs)
    except Exception:
        if eng != "auto":
            raise
        print("  cuda 加载失败,auto 降级 cpu int8 重试...")
        model_obj = WhisperModel(model, **engine.construction_kwargs("cpu"))

    print()
    print(f"模型 {model} 下载完成!")
    print("运行测试: ./test-mic.sh")

if __name__ == "__main__":
    main()
