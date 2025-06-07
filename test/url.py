import aiohttp
import asyncio
import ssl

async def unshorten_url(url: str) -> str:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, allow_redirects=True, ssl=ssl_context) as resp:
                return str(resp.url)
        except Exception as e:
            print(f"[unshorten_url] Failed to unshorten {url}: {e}")
            return url

# Usage
async def main():
    short_url = "https://short-link.me/13Rhs"
    print("🔗 Unshortened:", await unshorten_url(short_url))

asyncio.run(main())
