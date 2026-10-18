package gene.audio;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sedmelluq.discord.lavaplayer.player.AudioPlayerManager;
import com.sedmelluq.discord.lavaplayer.source.AudioSourceManager;
import com.sedmelluq.discord.lavaplayer.source.http.HttpAudioSourceManager;
import com.sedmelluq.discord.lavaplayer.tools.FriendlyException;
import com.sedmelluq.discord.lavaplayer.track.*;
import com.sedmelluq.discord.lavaplayer.track.playback.LocalAudioTrackExecutor;
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import org.apache.http.message.BasicHeader;

public class GeneYoutubeSource implements AudioSourceManager {
    private final String endpoint;
    private final String token;
    private final ObjectMapper json = new ObjectMapper();
    private final HttpClient client = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
        .connectTimeout(Duration.ofSeconds(5)).build();

    public GeneYoutubeSource(String endpoint, String token) {
        URI uri = URI.create(endpoint);
        if (!uri.getScheme().equals("http") || !uri.getHost().equals("127.0.0.1"))
            throw new IllegalArgumentException("The resolver must use loopback");
        this.endpoint = endpoint;
        this.token = token;
    }

    private JsonNode request(String route, String identifier) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(endpoint + route))
            .timeout(Duration.ofSeconds(55)).header("Authorization", "Bearer " + token)
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(Map.of("identifier", identifier))))
            .build();
        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200)
            throw new FriendlyException("This public YouTube track could not be loaded. Try another track.",
                                        FriendlyException.Severity.COMMON, null);
        return json.readTree(response.body());
    }

    @Override public String getSourceName() { return "youtube"; }

    @Override public AudioItem loadItem(AudioPlayerManager manager, AudioReference reference) {
        String id = reference.identifier;
        if (id == null || !(id.startsWith("ytsearch:") || id.startsWith("ytmsearch:")
                || id.matches("[A-Za-z0-9_-]{11}")
                || id.matches("https://(?:(?:www\\.|m\\.|music\\.)?youtube\\.com|youtu\\.be)/.*"))) return null;
        try {
            JsonNode result = request("/load", id);
            List<AudioTrack> tracks = new ArrayList<>();
            for (JsonNode row : result.path("tracks")) {
                AudioTrackInfo info = new AudioTrackInfo(row.path("title").asText(), row.path("author").asText(),
                    row.path("length").asLong(), row.path("identifier").asText(), false, row.path("uri").asText());
                tracks.add(new Track(info));
            }
            if (tracks.isEmpty()) return AudioReference.NO_TRACK;
            if (result.path("playlist").asBoolean())
                return new BasicAudioPlaylist(result.path("name").asText(), tracks, null, result.path("search").asBoolean());
            return tracks.get(0);
        } catch (FriendlyException e) { throw e;
        } catch (Exception e) {
            throw new FriendlyException("The YouTube resolver is unavailable. Try again shortly.",
                                        FriendlyException.Severity.COMMON, null);
        }
    }

    @Override public boolean isTrackEncodable(AudioTrack track) { return true; }
    @Override public void encodeTrack(AudioTrack track, DataOutput output) { }
    @Override public AudioTrack decodeTrack(AudioTrackInfo info, DataInput input) { return new Track(info); }
    @Override public void shutdown() { }

    private class Track extends DelegatedAudioTrack {
        Track(AudioTrackInfo info) { super(info); }
        @Override public AudioSourceManager getSourceManager() { return GeneYoutubeSource.this; }
        @Override protected AudioTrack makeShallowClone() { return new Track(trackInfo); }
        @Override public void process(LocalAudioTrackExecutor executor) throws Exception {
            JsonNode resolved = request("/resolve", trackInfo.identifier);
            URI uri = URI.create(resolved.path("url").asText());
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null
                    || !uri.getHost().endsWith(".googlevideo.com") || uri.getUserInfo() != null)
                throw new IllegalArgumentException("Unsupported media host");
            HttpAudioSourceManager http = new HttpAudioSourceManager();
            try {
                List<org.apache.http.Header> headers = new ArrayList<>();
                resolved.path("headers").fields().forEachRemaining(entry -> {
                    if (Set.of("user-agent", "referer", "origin").contains(entry.getKey().toLowerCase(Locale.ROOT)))
                        headers.add(new BasicHeader(entry.getKey(), entry.getValue().asText()));
                });
                http.configureBuilder(builder -> builder.setDefaultHeaders(headers));
                AudioItem item = http.loadItem(null, new AudioReference(uri.toString(), trackInfo.title));
                if (!(item instanceof InternalAudioTrack))
                    throw new FriendlyException("The audio stream could not be decoded.", FriendlyException.Severity.COMMON, null);
                processDelegate((InternalAudioTrack) item, executor);
            } catch (Exception e) {
                throw new FriendlyException("YouTube audio could not be streamed. Try again or another public track.",
                                            FriendlyException.Severity.COMMON, null);
            } finally { http.shutdown(); }
        }
    }
}
