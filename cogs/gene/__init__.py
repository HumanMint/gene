from .gene import Gene


async def setup(bot):
    await bot.add_cog(Gene(bot))

