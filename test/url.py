import httpx
import asyncio

async def unshorten_url(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0, verify=False) as client:
            resp = await client.get(url)
            return str(resp.url)
    except Exception as e:
        print(f"[unshorten_url] Failed to unshorten {url}: {e}")
        return url

# Test
async def main():
    short_url = "neomechanical.com:10000"
    print("🔗 Unshortened:", await unshorten_url(short_url))

asyncio.run(main())