package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/azylman/mirrormere/internal/tasks"
	"github.com/azylman/mirrormere/internal/tasks/adapters"
)

const defaultTasksShowCompleted = 3

// TasksConfig holds parsed configuration for TasksProvider.
type TasksConfig struct {
	ListID        string
	ListName      string
	Source        string
	ShowCompleted int
}

// TasksProvider implements Provider for the built-in tasks widget backed by upstream adapters.
type TasksProvider struct {
	mu      sync.RWMutex
	cfg     TasksConfig
	adapter adapters.Adapter
}

func init() {
	registerBuiltin("tasks", func() Provider {
		return NewTasksProvider()
	})
}

// NewTasksProvider constructs a new TasksProvider.
func NewTasksProvider() *TasksProvider {
	return &TasksProvider{}
}

func parseTasksConfig(config map[string]any, defaultID string) TasksConfig {
	cfg := TasksConfig{
		ListID:        defaultID,
		ShowCompleted: defaultTasksShowCompleted,
	}

	if raw, ok := config["list_id"].(string); ok && raw != "" {
		cfg.ListID = raw
	}
	if raw, ok := config["list_name"].(string); ok && raw != "" {
		cfg.ListName = raw
	} else {
		cfg.ListName = cfg.ListID
	}
	if raw, ok := config["source"].(string); ok {
		cfg.Source = strings.TrimSpace(raw)
	}
	if raw, ok := config["show_completed"]; ok {
		switch v := raw.(type) {
		case int:
			cfg.ShowCompleted = v
		case float64:
			cfg.ShowCompleted = int(v)
		case string:
			if n, err := strconv.Atoi(v); err == nil {
				cfg.ShowCompleted = n
			}
		}
	}
	if cfg.ShowCompleted < 0 {
		cfg.ShowCompleted = 0
	}
	return cfg
}

func applyGTasksCredentialsFile(filePath string, clientID, clientSecret, refreshToken, token *string) {
	if filePath == "" {
		return
	}
	data, err := os.ReadFile(filePath)
	if err != nil {
		return
	}
	var uCreds struct {
		ClientID     string `json:"client_id"`
		ClientSecret string `json:"client_secret"`
		RefreshToken string `json:"refresh_token"`
		Token        string `json:"token"`
		Installed    struct {
			ClientID     string `json:"client_id"`
			ClientSecret string `json:"client_secret"`
		} `json:"installed"`
		Web struct {
			ClientID     string `json:"client_id"`
			ClientSecret string `json:"client_secret"`
		} `json:"web"`
	}
	if err := json.Unmarshal(data, &uCreds); err != nil {
		return
	}
	if *clientID == "" {
		switch {
		case uCreds.ClientID != "":
			*clientID = uCreds.ClientID
		case uCreds.Installed.ClientID != "":
			*clientID = uCreds.Installed.ClientID
		case uCreds.Web.ClientID != "":
			*clientID = uCreds.Web.ClientID
		}
	}
	if *clientSecret == "" {
		switch {
		case uCreds.ClientSecret != "":
			*clientSecret = uCreds.ClientSecret
		case uCreds.Installed.ClientSecret != "":
			*clientSecret = uCreds.Installed.ClientSecret
		case uCreds.Web.ClientSecret != "":
			*clientSecret = uCreds.Web.ClientSecret
		}
	}
	if *refreshToken == "" && uCreds.RefreshToken != "" {
		*refreshToken = uCreds.RefreshToken
	}
	if *token == "" && uCreds.Token != "" {
		*token = uCreds.Token
	}
}

func buildGTasksAdapter(config map[string]any, cfg TasksConfig, opts InitOptions) (adapters.Adapter, error) {
	gtasksCfg, ok := config["gtasks"].(map[string]any)
	if !ok || gtasksCfg == nil {
		return nil, errors.New("tasks provider with source 'gtasks' requires 'gtasks' config block")
	}
	taskListID, ok := gtasksCfg["tasklist_id"].(string)
	if !ok || taskListID == "" {
		return nil, errors.New("tasks provider with source 'gtasks' requires 'gtasks.tasklist_id'")
	}
	baseURL := ""
	if rawURL, ok := gtasksCfg["base_url"].(string); ok {
		baseURL = rawURL
	}
	tokenURL := ""
	if rawURL, ok := gtasksCfg["token_url"].(string); ok {
		tokenURL = rawURL
	}

	clientID := resolveTaskSecret(opts, gtasksCfg, "gtasks", "client_id", "client_id_env")
	clientSecret := resolveTaskSecret(opts, gtasksCfg, "gtasks", "client_secret", "client_secret_env")
	refreshToken := resolveTaskSecret(opts, gtasksCfg, "gtasks", "refresh_token", "refresh_token_env")

	token := resolveTaskSecret(opts, gtasksCfg, "gtasks", "token", "token_env")
	if token == "" {
		token = strings.TrimSpace(opts.Token)
	}

	applyGTasksCredentialsFile(resolveTaskSecret(opts, gtasksCfg, "gtasks", "credentials_file", "credentials_file_env"), &clientID, &clientSecret, &refreshToken, &token)
	applyGTasksCredentialsFile(resolveTaskSecret(opts, gtasksCfg, "gtasks", "client_secret_file", "client_secret_file_env"), &clientID, &clientSecret, &refreshToken, &token)

	ad, err := adapters.NewGTasksAdapter(adapters.GTasksAdapterConfig{
		TaskListID:   taskListID,
		Token:        token,
		ClientID:     clientID,
		ClientSecret: clientSecret,
		RefreshToken: refreshToken,
		TokenURL:     tokenURL,
		ListName:     cfg.ListName,
		BaseURL:      baseURL,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create gtasks adapter: %w", err)
	}
	return ad, nil
}

func buildTodoistAdapter(config map[string]any, opts InitOptions) (adapters.Adapter, error) {
	todoistCfg, ok := config["todoist"].(map[string]any)
	if !ok || todoistCfg == nil {
		return nil, errors.New("tasks provider with source 'todoist' requires 'todoist' config block")
	}

	token := resolveTaskSecret(opts, todoistCfg, "todoist", "token", "token_env")
	if token == "" {
		token = strings.TrimSpace(opts.Token)
	}
	if token == "" {
		token = opts.GetSecret("TODOIST_API_TOKEN")
		if token == "" {
			token = os.Getenv("TODOIST_API_TOKEN")
		}
	}
	if token == "" {
		token = opts.GetSecret("TODOIST_TOKEN")
		if token == "" {
			token = os.Getenv("TODOIST_TOKEN")
		}
	}
	token = strings.TrimSpace(token)
	if token == "" {
		return nil, errors.New("tasks provider with source 'todoist' requires a valid token (via 'token', 'token_env', or environment)")
	}

	projectID := ""
	if raw, ok := todoistCfg["project_id"].(string); ok {
		projectID = strings.TrimSpace(raw)
	}
	projectName := ""
	if raw, ok := todoistCfg["project_name"].(string); ok {
		projectName = strings.TrimSpace(raw)
	}
	baseURL := ""
	if raw, ok := todoistCfg["base_url"].(string); ok {
		baseURL = strings.TrimSpace(raw)
	}

	explicitListName := ""
	if raw, ok := config["list_name"].(string); ok {
		explicitListName = strings.TrimSpace(raw)
	}

	ad, err := adapters.NewTodoistAdapter(adapters.TodoistAdapterConfig{
		ProjectID:   projectID,
		ProjectName: projectName,
		Token:       token,
		BaseURL:     baseURL,
		ListName:    explicitListName,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create todoist adapter: %w", err)
	}
	return ad, nil
}

// Init configures the tasks provider and instantiates external adapters if declared.
func (p *TasksProvider) Init(ctx context.Context, config map[string]any, opts InitOptions) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	p.cfg = parseTasksConfig(config, opts.ID)

	// Instantiate external source adapter if not already injected
	if p.adapter == nil {
		switch p.cfg.Source {
		case "gtasks":
			ad, err := buildGTasksAdapter(config, p.cfg, opts)
			if err != nil {
				return err
			}
			p.adapter = ad

		case "todoist":
			ad, err := buildTodoistAdapter(config, opts)
			if err != nil {
				return err
			}
			p.adapter = ad

		default:
			return fmt.Errorf("tasks provider requires valid upstream source ('gtasks' or 'todoist'), got %q", p.cfg.Source)
		}
	}

	return nil
}

// Fetch retrieves the tasks snapshot from the configured upstream adapter.
func (p *TasksProvider) Fetch(ctx context.Context) (any, error) {
	p.mu.RLock()
	cfg := p.cfg
	adapter := p.adapter
	p.mu.RUnlock()

	if adapter == nil {
		return nil, errors.New("tasks provider not initialized: nil adapter")
	}

	list, items, err := adapter.FetchList(ctx)
	if err != nil {
		return nil, fmt.Errorf("task list sync failed for %s (%s): %w", cfg.ListID, adapter.Name(), err)
	}
	if list == nil {
		return nil, fmt.Errorf("task list sync failed for %s (%s): adapter returned nil list", cfg.ListID, adapter.Name())
	}
	if cfg.ListID != "" && list.ID != cfg.ListID {
		list.ID = cfg.ListID
		for i := range items {
			items[i].ListID = cfg.ListID
		}
	}

	return tasks.BuildSnapshot(*list, items, cfg.ShowCompleted), nil
}

// Subscribe returns nil as upstream task providers poll on their configured refresh interval.
func (p *TasksProvider) Subscribe(ctx context.Context, eventSink chan<- WidgetPayload) error {
	return nil
}

// Shutdown cleanly stops the provider.
func (p *TasksProvider) Shutdown(ctx context.Context) error {
	return nil
}

// CompleteItem completes a task item upstream via the configured adapter.
func (p *TasksProvider) CompleteItem(ctx context.Context, itemID string) error {
	p.mu.RLock()
	adapter := p.adapter
	p.mu.RUnlock()

	if adapter == nil {
		return errors.New("tasks provider not initialized: nil adapter")
	}

	completer, ok := adapter.(adapters.Completer)
	if !ok {
		return tasks.ErrCompletionUnsupported
	}
	if err := completer.CompleteItem(ctx, itemID); err != nil {
		if errors.Is(err, adapters.ErrUpstreamNotFound) {
			return tasks.ErrItemNotFound
		}
		return fmt.Errorf("upstream task completion failed: %w", err)
	}

	return nil
}

// ListID returns the configured list ID for this tasks provider.
func (p *TasksProvider) ListID() string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.cfg.ListID
}

func resolveTaskSecret(opts InitOptions, subCfg map[string]any, prefix, field, envField string) string {
	val := opts.GetSecret(prefix + "." + field)
	if val == "" {
		val = opts.GetSecret(field)
	}
	if val == "" {
		if raw, ok := subCfg[field].(string); ok {
			val = raw
		}
	}
	if val == "" && envField != "" {
		if envKey, ok := subCfg[envField].(string); ok && envKey != "" {
			val = opts.GetSecret(envKey)
			if val == "" {
				val = os.Getenv(envKey)
			}
		}
	}
	return strings.TrimSpace(val)
}
