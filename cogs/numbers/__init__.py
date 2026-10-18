from .numbers import Numbers


async def setup(bot):
    await bot.add_cog(Numbers(bot))

