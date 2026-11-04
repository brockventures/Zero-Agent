package provider

import (
	"fmt"
	"net/url"
	"os"
	"strings"
)

func resolveRawSingleAlbumURL(idx int, a AlbumSource, isLegacySingle bool, opts InitOptions) string {
	if isLegacySingle {
		if sec := opts.GetSecret("album_url"); sec != "" {
			return strings.TrimSpace(sec)
		}
		if sec := opts.GetSecret("share_url"); sec != "" {
			return strings.TrimSpace(sec)
		}
	}
	if sec := opts.GetSecret(fmt.Sprintf("albums[%d].url", idx)); sec != "" {
		return strings.TrimSpace(sec)
	}
	if a.URLEnv != "" {
		if sec := opts.GetSecret(a.URLEnv); sec != "" {
			return strings.TrimSpace(sec)
		}
		if env := os.Getenv(a.URLEnv); env != "" {
			return strings.TrimSpace(env)
		}
		return ""
	}
	return a.URL
}

func resolveSingleAlbumURL(idx int, a AlbumSource, isLegacySingle bool, opts InitOptions) (string, error) {
	resolvedURL := resolveRawSingleAlbumURL(idx, a, isLegacySingle, opts)
	if resolvedURL == "" && a.URLEnv != "" {
		envName := "url_env"
		if isLegacySingle {
			envName = "album_url_env"
		}
		return "", fmt.Errorf("photo-carousel: environment variable %q specified in %q is not set or empty", a.URLEnv, envName)
	}

	if resolvedURL == "" {
		return "", fmt.Errorf("photo-carousel: resolved album URL is empty")
	}

	parsedU, err := url.Parse(resolvedURL)
	if err != nil || (parsedU.Scheme != "http" && parsedU.Scheme != "https") || parsedU.Host == "" {
		return "", fmt.Errorf("photo-carousel: invalid album URL %q", redactURL(resolvedURL))
	}

	return resolvedURL, nil
}

func resolveAlbumSources(parsed PhotosConfig, opts InitOptions) ([]AlbumSource, error) {
	if len(parsed.Albums) == 0 {
		return nil, fmt.Errorf("photo-carousel: no albums configured")
	}

	resolved := make([]AlbumSource, len(parsed.Albums))
	for idx, a := range parsed.Albums {
		resolvedURL, err := resolveSingleAlbumURL(idx, a, parsed.isLegacy, opts)
		if err != nil {
			return nil, err
		}
		a.ResolvedURL = resolvedURL
		resolved[idx] = a
	}
	return resolved, nil
}

// parsePhotosConfig parses and validates instance configuration.
func parsePhotosConfig(cfg map[string]any, _ InitOptions) (PhotosConfig, error) {
	if cfg == nil {
		return PhotosConfig{}, fmt.Errorf("photo-carousel: configuration is required")
	}

	var res PhotosConfig
	if cd, ok := cfg["cache_dir"].(string); ok && strings.TrimSpace(cd) != "" {
		res.CacheDir = strings.TrimSpace(cd)
	}

	if rawAlbums, ok := cfg["albums"]; ok && rawAlbums != nil {
		albums, err := parseAlbumsList(rawAlbums)
		if err != nil {
			return PhotosConfig{}, err
		}
		res.Albums = albums
		if len(albums) > 0 {
			res.Source = albums[0].Source
			res.AlbumURL = albums[0].URL
			res.AlbumURLEnv = albums[0].URLEnv
		}
		return res, nil
	}

	return parseLegacyPhotosConfig(cfg, res.CacheDir)
}

func mapString(m map[string]any, key string) string {
	if v, ok := m[key].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}

func parseAlbumsList(raw any) ([]AlbumSource, error) {
	slice, ok := raw.([]any)
	if !ok || len(slice) == 0 {
		return nil, fmt.Errorf("photo-carousel: 'albums' must contain at least one album")
	}

	albums := make([]AlbumSource, 0, len(slice))
	for idx, item := range slice {
		itemMap, ok := item.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("photo-carousel: album at index %d must be an object", idx)
		}
		name := mapString(itemMap, "name")
		if name == "" {
			return nil, fmt.Errorf("photo-carousel: album at index %d requires non-empty 'name'", idx)
		}

		source := mapString(itemMap, "source")
		urlStr := mapString(itemMap, "url")
		urlEnvStr := mapString(itemMap, "url_env")

		if source == "" {
			if urlStr != "" || urlEnvStr != "" {
				source = PhotoSourceShareURL
			} else {
				return nil, fmt.Errorf("photo-carousel: album %q requires 'source'", name)
			}
		} else if source != PhotoSourceShareURL && source != PhotoSourceEmberBridge {
			return nil, fmt.Errorf("photo-carousel: 'source' must be either %q or %q", PhotoSourceShareURL, PhotoSourceEmberBridge)
		}

		if urlStr != "" && urlEnvStr != "" {
			return nil, fmt.Errorf("photo-carousel: 'url' and 'url_env' are mutually exclusive in album %q", name)
		}
		if urlStr == "" && urlEnvStr == "" {
			return nil, fmt.Errorf("photo-carousel: album %q requires either 'url' or 'url_env'", name)
		}

		albums = append(albums, AlbumSource{
			Name:   name,
			Source: source,
			URL:    urlStr,
			URLEnv: urlEnvStr,
		})
	}
	return albums, nil
}

func parseLegacyPhotosConfig(cfg map[string]any, cacheDir string) (PhotosConfig, error) {
	var source string
	if sourceVal, ok := cfg["source"].(string); ok {
		source = strings.TrimSpace(sourceVal)
	}
	if source == "" {
		if cfg["share_url"] != nil || cfg["share_url_env"] != nil {
			source = PhotoSourceShareURL
		} else {
			return PhotosConfig{}, fmt.Errorf("photo-carousel: 'source' is required and must be either %q or %q", PhotoSourceShareURL, PhotoSourceEmberBridge)
		}
	} else if source != PhotoSourceShareURL && source != PhotoSourceEmberBridge {
		return PhotosConfig{}, fmt.Errorf("photo-carousel: 'source' is required and must be either %q or %q", PhotoSourceShareURL, PhotoSourceEmberBridge)
	}

	var albumURL, albumURLEnv string
	if v, ok := cfg["album_url"].(string); ok && strings.TrimSpace(v) != "" {
		albumURL = strings.TrimSpace(v)
	} else if v, ok := cfg["share_url"].(string); ok {
		albumURL = strings.TrimSpace(v)
	}

	if v, ok := cfg["album_url_env"].(string); ok && strings.TrimSpace(v) != "" {
		albumURLEnv = strings.TrimSpace(v)
	} else if v, ok := cfg["share_url_env"].(string); ok {
		albumURLEnv = strings.TrimSpace(v)
	}

	if albumURL != "" && albumURLEnv != "" {
		return PhotosConfig{}, fmt.Errorf("photo-carousel: 'album_url' and 'album_url_env' are mutually exclusive")
	}
	if albumURL == "" && albumURLEnv == "" {
		return PhotosConfig{}, fmt.Errorf("photo-carousel requires either 'album_url' or 'album_url_env'")
	}

	res := PhotosConfig{
		Source:      source,
		AlbumURL:    albumURL,
		AlbumURLEnv: albumURLEnv,
		CacheDir:    cacheDir,
		isLegacy:    true,
		Albums: []AlbumSource{
			{
				Name:   "Shared Album",
				Source: source,
				URL:    albumURL,
				URLEnv: albumURLEnv,
			},
		},
	}
	return res, nil
}
