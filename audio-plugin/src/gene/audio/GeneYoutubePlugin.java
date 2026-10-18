package gene.audio;

import com.sedmelluq.discord.lavaplayer.player.AudioPlayerManager;
import dev.arbjerg.lavalink.api.AudioPlayerManagerConfiguration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/** An external Lavalink source. Does not modify Red or its Audio cog. */
@Component
public class GeneYoutubePlugin implements AudioPlayerManagerConfiguration {
    private final String url;
    private final String token;
    private final String cache;

    public GeneYoutubePlugin(@Value("${plugins.geneYoutube.url:http://127.0.0.1:2334}") String url,
                             @Value("${plugins.geneYoutube.token}") String token,
                             @Value("${plugins.geneYoutube.cache}") String cache) {
        this.url = url;
        this.token = token;
        this.cache = cache;
    }

    @Override public AudioPlayerManager configure(AudioPlayerManager manager) {
        manager.registerSourceManager(new GeneYoutubeSource(url, token, cache));
        return manager;
    }
}
