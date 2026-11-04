package adapters

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/azylman/mirrormere/internal/tasks"
)

const (
	defaultGoogleTasksBaseURL                = "https://tasks.googleapis.com"
	defaultGoogleOAuthTokenURL               = "https://oauth2.googleapis.com/token"
	maxGoogleTasksPages, googleTasksPageSize = 10, 100
)

// GTasksAdapterConfig holds configuration for the Google Tasks API adapter.
type GTasksAdapterConfig struct {
	TaskListID, Token, ClientID, ClientSecret, RefreshToken, TokenURL, ListName, BaseURL string
	Client                                                                               *http.Client
}

// GTasksAdapter ingests task lists and items from the Google Tasks API.
type GTasksAdapter struct {
	taskListID, token, clientID, clientSecret, refreshToken, tokenURL, listName, baseURL string
	client                                                                               *http.Client
	tokenMu                                                                              sync.Mutex
	cachedToken                                                                          string
	tokenExpiry                                                                          time.Time
}

// NewGTasksAdapter creates a validated GTasksAdapter.
func NewGTasksAdapter(cfg GTasksAdapterConfig) (*GTasksAdapter, error) {
	taskListID := strings.TrimSpace(cfg.TaskListID)
	if taskListID == "" {
		return nil, errors.New("tasklist_id is required")
	}
	baseURL, err := validateHTTPURL(cfg.BaseURL, defaultGoogleTasksBaseURL, "base_url")
	if err != nil {
		return nil, err
	}
	tokenURL, err := validateHTTPURL(cfg.TokenURL, defaultGoogleOAuthTokenURL, "token_url")
	if err != nil {
		return nil, err
	}

	return &GTasksAdapter{
		taskListID:   taskListID,
		token:        strings.TrimSpace(cfg.Token),
		clientID:     strings.TrimSpace(cfg.ClientID),
		clientSecret: strings.TrimSpace(cfg.ClientSecret),
		refreshToken: strings.TrimSpace(cfg.RefreshToken),
		tokenURL:     tokenURL,
		listName:     strings.TrimSpace(cfg.ListName),
		baseURL:      baseURL,
		client:       defaultHTTPClient(cfg.Client),
	}, nil
}

var _ Completer = (*GTasksAdapter)(nil)

func (a *GTasksAdapter) Name() string { return "gtasks" }

type gtasksListMetadata struct {
	ID      string `json:"id"`
	Title   string `json:"title"`
	Updated string `json:"updated"`
}

type gtasksItem struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Updated   string `json:"updated"`
	Status    string `json:"status"` // "needsAction" or "completed"
	Due       string `json:"due"`    // RFC 3339 timestamp e.g. "2026-09-27T00:00:00.000Z"
	Position  string `json:"position"`
	Parent    string `json:"parent"`
	Notes     string `json:"notes"`
	Deleted   bool   `json:"deleted"`
	Hidden    bool   `json:"hidden"`
	Completed string `json:"completed"`
}

type gtasksListResponse struct {
	Kind          string       `json:"kind"`
	Items         []gtasksItem `json:"items"`
	NextPageToken string       `json:"nextPageToken"`
}

func (a *GTasksAdapter) fetchListMetadata(ctx context.Context) (string, time.Time, error) {
	listTitle, listUpdatedAt := a.listName, time.Now().UTC()

	metaURL := fmt.Sprintf("%s/tasks/v1/users/@me/lists/%s", a.baseURL, url.PathEscape(a.taskListID))
	metaBody, metaErr := a.doGet(ctx, metaURL)
	if metaErr != nil {
		if errors.Is(metaErr, ErrUnauthorized) || errors.Is(metaErr, ErrUpstreamNotFound) {
			return "", time.Time{}, metaErr
		}
		if listTitle == "" {
			listTitle = a.taskListID
		}
		return listTitle, listUpdatedAt, nil
	}

	var meta gtasksListMetadata
	if jsonErr := json.Unmarshal(metaBody, &meta); jsonErr == nil {
		if meta.Title != "" && listTitle == "" {
			listTitle = meta.Title
		}
		if meta.Updated != "" {
			if t, parseErr := time.Parse(time.RFC3339Nano, meta.Updated); parseErr == nil {
				listUpdatedAt = t.UTC()
			}
		}
	}

	if listTitle == "" {
		listTitle = a.taskListID
	}
	return listTitle, listUpdatedAt, nil
}

func convertGTaskItems(orderedGTasks []gtasksItem, taskListID string, now time.Time) []tasks.ListItem {
	items := make([]tasks.ListItem, 0, len(orderedGTasks))

	for _, gt := range orderedGTasks {
		done := strings.EqualFold(gt.Status, "completed")
		var dueDate *string
		if len(gt.Due) >= 10 {
			datePart := gt.Due[:10]
			if _, parseErr := time.Parse("2006-01-02", datePart); parseErr == nil {
				dueDate = &datePart
			}
		}

		itemUpdatedAt := now
		if gt.Updated != "" {
			if t, err := time.Parse(time.RFC3339Nano, gt.Updated); err == nil {
				itemUpdatedAt = t.UTC()
			}
		}

		items = append(items, tasks.ListItem{
			ID:        gt.ID,
			ListID:    taskListID,
			Title:     gt.Title,
			Done:      done,
			Position:  len(items),
			DueDate:   dueDate,
			CreatedAt: itemUpdatedAt,
			UpdatedAt: itemUpdatedAt,
		})
	}
	return items
}

// FetchList retrieves task list metadata and items from the Google Tasks API.
func (a *GTasksAdapter) FetchList(ctx context.Context) (*tasks.List, []tasks.ListItem, error) {
	listTitle, listUpdatedAt, err := a.fetchListMetadata(ctx)
	if err != nil {
		return nil, nil, err
	}

	allGTasks := make([]gtasksItem, 0, 64)
	seenTokens := make(map[string]bool)
	pageToken := ""

	for range maxGoogleTasksPages {
		queryParams := url.Values{}
		queryParams.Set("showCompleted", "true")
		queryParams.Set("showHidden", "true")
		queryParams.Set("maxResults", strconv.Itoa(googleTasksPageSize))
		if pageToken != "" {
			queryParams.Set("pageToken", pageToken)
		}

		tasksURL := fmt.Sprintf("%s/tasks/v1/lists/%s/tasks?%s",
			a.baseURL, url.PathEscape(a.taskListID), queryParams.Encode())

		body, err := a.doGet(ctx, tasksURL)
		if err != nil {
			return nil, nil, fmt.Errorf("failed to fetch tasks from google tasks: %w", err)
		}

		var pageResp gtasksListResponse
		if err := json.Unmarshal(body, &pageResp); err != nil {
			return nil, nil, fmt.Errorf("failed to parse google tasks json response: %w", err)
		}
		allGTasks = append(allGTasks, pageResp.Items...)

		if pageToken = strings.TrimSpace(pageResp.NextPageToken); pageToken == "" || seenTokens[pageToken] {
			break
		}
		seenTokens[pageToken] = true
	}

	activeItems := make([]gtasksItem, 0, len(allGTasks))
	for _, gt := range allGTasks {
		if !gt.Deleted {
			activeItems = append(activeItems, gt)
		}
	}

	orderedGTasks := orderGTasks(activeItems)
	now := time.Now().UTC()
	items := convertGTaskItems(orderedGTasks, a.taskListID, now)

	return &tasks.List{
		ID:        a.taskListID,
		Name:      listTitle,
		Source:    "gtasks",
		Sections:  []string{},
		UpdatedAt: listUpdatedAt,
	}, items, nil
}

func orderGTasks(items []gtasksItem) []gtasksItem {
	if len(items) <= 1 {
		return items
	}
	itemByID := make(map[string]gtasksItem, len(items))
	childrenByParent := make(map[string][]gtasksItem)
	var topLevel []gtasksItem

	for _, item := range items {
		itemByID[item.ID] = item
	}
	for _, item := range items {
		if item.Parent == "" {
			topLevel = append(topLevel, item)
		} else if _, exists := itemByID[item.Parent]; exists {
			childrenByParent[item.Parent] = append(childrenByParent[item.Parent], item)
		} else {
			topLevel = append(topLevel, item)
		}
	}

	sortGTasks := func(slice []gtasksItem) {
		sort.SliceStable(slice, func(i, j int) bool { return slice[i].Position < slice[j].Position })
	}
	sortGTasks(topLevel)
	for parentID := range childrenByParent {
		sortGTasks(childrenByParent[parentID])
	}

	ordered := make([]gtasksItem, 0, len(items))
	visited := make(map[string]bool, len(items))

	var traverse func(item gtasksItem)
	traverse = func(item gtasksItem) {
		if visited[item.ID] {
			return
		}
		visited[item.ID] = true
		ordered = append(ordered, item)
		for _, child := range childrenByParent[item.ID] {
			traverse(child)
		}
	}
	for _, item := range topLevel {
		traverse(item)
	}
	for _, item := range items {
		if !visited[item.ID] {
			traverse(item)
		}
	}
	return ordered
}

type oauthTokenResponse struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int    `json:"expires_in"`
	TokenType   string `json:"token_type"`
}

func (a *GTasksAdapter) getAccessToken(ctx context.Context, forceRefresh bool) (string, error) {
	a.tokenMu.Lock()
	defer a.tokenMu.Unlock()

	if a.refreshToken == "" {
		return a.token, nil
	}
	if !forceRefresh && a.cachedToken != "" && time.Now().Add(60*time.Second).Before(a.tokenExpiry) {
		return a.cachedToken, nil
	}

	form := url.Values{
		"grant_type":    {"refresh_token"},
		"client_id":     {a.clientID},
		"client_secret": {a.clientSecret},
		"refresh_token": {a.refreshToken},
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", fmt.Errorf("failed to create oauth token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")

	resp, err := a.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("oauth token request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 64*1024))
	if readErr != nil {
		return "", fmt.Errorf("failed to read oauth token response: %w", readErr)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("oauth token refresh failed with status %d: %s", resp.StatusCode, string(body))
	}

	var tokenResp oauthTokenResponse
	if err := json.Unmarshal(body, &tokenResp); err != nil {
		return "", fmt.Errorf("failed to parse oauth token response: %w", err)
	}
	if tokenResp.AccessToken == "" {
		return "", errors.New("oauth token response missing access_token")
	}

	expiresIn := tokenResp.ExpiresIn
	if expiresIn <= 0 {
		expiresIn = 3600
	}
	a.cachedToken = tokenResp.AccessToken
	a.tokenExpiry = time.Now().Add(time.Duration(expiresIn) * time.Second)
	return a.cachedToken, nil
}

func (a *GTasksAdapter) doGet(ctx context.Context, targetURL string) ([]byte, error) {
	return a.doGetWithRetry(ctx, targetURL, false)
}

func (a *GTasksAdapter) doGetWithRetry(ctx context.Context, targetURL string, isRetry bool) ([]byte, error) {
	token, err := a.getAccessToken(ctx, false)
	if err != nil {
		return nil, fmt.Errorf("failed to obtain gtasks access token: %w", err)
	}

	body, status, err := doBearerGet(ctx, a.client, targetURL, token, "google tasks request")
	if status == http.StatusUnauthorized && !isRetry && a.refreshToken != "" {
		if _, refreshErr := a.getAccessToken(ctx, true); refreshErr == nil {
			return a.doGetWithRetry(ctx, targetURL, true)
		}
	}
	return body, err
}

// CompleteItem completes a task in Google Tasks via PATCH /tasks/v1/lists/{taskListID}/tasks/{itemID}.
func (a *GTasksAdapter) CompleteItem(ctx context.Context, itemID string) error {
	itemID = strings.TrimSpace(itemID)
	if itemID == "" {
		return errors.New("item ID is required")
	}
	if strings.Contains(itemID, "/") || strings.Contains(itemID, "..") {
		return errors.New("invalid item ID")
	}

	targetURL := fmt.Sprintf("%s/tasks/v1/lists/%s/tasks/%s", a.baseURL, url.PathEscape(a.taskListID), url.PathEscape(itemID))
	return a.doPatchWithRetry(ctx, targetURL, false)
}

func (a *GTasksAdapter) doPatchWithRetry(ctx context.Context, targetURL string, isRetry bool) error {
	token, err := a.getAccessToken(ctx, false)
	if err != nil {
		return fmt.Errorf("failed to obtain gtasks access token: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPatch, targetURL, strings.NewReader(`{"status":"completed"}`))
	if err != nil {
		return fmt.Errorf("failed to create http request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}

	resp, err := a.client.Do(req)
	if err != nil {
		return fmt.Errorf("google tasks patch request failed: %w", err)
	}

	if resp.StatusCode == http.StatusUnauthorized && !isRetry && a.refreshToken != "" {
		_ = resp.Body.Close()
		if _, refreshErr := a.getAccessToken(ctx, true); refreshErr == nil {
			return a.doPatchWithRetry(ctx, targetURL, true)
		}
		return mapUpstreamStatus(resp.StatusCode, targetURL)
	}
	defer func() { _ = resp.Body.Close() }()

	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if err != nil {
		return fmt.Errorf("failed to drain response body: %w", err)
	}
	return mapUpstreamStatus(resp.StatusCode, targetURL)
}
