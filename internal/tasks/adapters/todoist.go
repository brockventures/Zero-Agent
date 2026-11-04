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
	"time"

	"github.com/azylman/mirrormere/internal/tasks"
)

const (
	defaultTodoistBaseURL = "https://api.todoist.com"
	maxTodoistPages       = 10
)

var (
	_ Adapter   = (*TodoistAdapter)(nil)
	_ Completer = (*TodoistAdapter)(nil)
)

// TodoistAdapterConfig holds configuration for the Todoist tasks adapter.
type TodoistAdapterConfig struct {
	ProjectID, ProjectName, Token, BaseURL, ListName string
	Client                                           *http.Client
}

// TodoistAdapter ingests task lists and items from the Todoist REST and Sync APIs.
type TodoistAdapter struct {
	projectID, projectName, token, baseURL, listName string
	client                                           *http.Client
}

// NewTodoistAdapter creates a validated TodoistAdapter.
func NewTodoistAdapter(cfg TodoistAdapterConfig) (*TodoistAdapter, error) {
	token := strings.TrimSpace(cfg.Token)
	if token == "" {
		return nil, errors.New("token is required")
	}
	baseURL, err := validateHTTPURL(cfg.BaseURL, defaultTodoistBaseURL, "base_url")
	if err != nil {
		return nil, err
	}
	return &TodoistAdapter{
		projectID:   strings.TrimSpace(cfg.ProjectID),
		projectName: strings.TrimSpace(cfg.ProjectName),
		token:       token,
		baseURL:     baseURL,
		listName:    strings.TrimSpace(cfg.ListName),
		client:      defaultHTTPClient(cfg.Client),
	}, nil
}

func (a *TodoistAdapter) Name() string { return "todoist" }

// flexibleString supports unmarshaling JSON values that can be strings, numbers, or null.
type flexibleString string

func (f *flexibleString) UnmarshalJSON(b []byte) error {
	var raw any
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	switch v := raw.(type) {
	case string:
		*f = flexibleString(strings.TrimSpace(v))
	case float64:
		*f = flexibleString(strconv.FormatInt(int64(v), 10))
	case nil:
		*f = ""
	default:
		*f = flexibleString(strings.TrimSpace(fmt.Sprintf("%v", v)))
	}
	return nil
}

type todoistProject struct {
	ID             flexibleString `json:"id"`
	Name           string         `json:"name"`
	InboxProject   bool           `json:"inbox_project"`
	IsInboxProject bool           `json:"is_inbox_project"`
	Order          int            `json:"order"`
}

func (p todoistProject) IsInbox() bool { return p.InboxProject || p.IsInboxProject }

type todoistSection struct {
	ID        flexibleString `json:"id"`
	ProjectID flexibleString `json:"project_id"`
	Name      string         `json:"name"`
	Order     int            `json:"order"`
}

type todoistDue struct {
	Date     string `json:"date"`
	Datetime string `json:"datetime"`
}

type todoistTask struct {
	ID          flexibleString `json:"id"`
	ProjectID   flexibleString `json:"project_id"`
	SectionID   flexibleString `json:"section_id"`
	Content     string         `json:"content"`
	Description string         `json:"description"`
	Checked     bool           `json:"checked"`
	IsCompleted bool           `json:"is_completed"`
	ChildOrder  int            `json:"child_order"`
	Order       int            `json:"order"`
	ParentID    flexibleString `json:"parent_id"`
	Priority    int            `json:"priority"`
	Due         *todoistDue    `json:"due"`
	CreatedAt   string         `json:"created_at"`
	AddedAt     string         `json:"added_at"`
}

func (t todoistTask) EffectiveDone() bool { return t.Checked || t.IsCompleted }
func (t todoistTask) EffectiveOrder() int {
	if t.ChildOrder != 0 {
		return t.ChildOrder
	}
	return t.Order
}

func (t todoistTask) EffectiveCreatedAt() string {
	if t.CreatedAt != "" {
		return t.CreatedAt
	}
	return t.AddedAt
}

type todoistCompletedItem struct {
	ID            flexibleString `json:"id"`
	TaskID        flexibleString `json:"task_id"`
	Content       string         `json:"content"`
	CompletedAt   string         `json:"completed_at"`
	CompletedDate string         `json:"completed_date"`
	ProjectID     flexibleString `json:"project_id"`
	SectionID     flexibleString `json:"section_id"`
}

type todoistCompletedResponse struct {
	Items []todoistCompletedItem `json:"items"`
}

func unmarshalPageOrSlice[T any](body []byte) ([]T, string, error) {
	var envelope struct {
		Results    []T    `json:"results"`
		NextCursor string `json:"next_cursor"`
	}
	if err := json.Unmarshal(body, &envelope); err == nil && envelope.Results != nil {
		return envelope.Results, strings.TrimSpace(envelope.NextCursor), nil
	}
	var direct []T
	if err := json.Unmarshal(body, &direct); err == nil {
		return direct, "", nil
	}
	return nil, "", errors.New("failed to decode response as results envelope or slice")
}

func fetchPaginated[T any](ctx context.Context, a *TodoistAdapter, rawURL, resourceLabel string) ([]T, error) {
	var all []T
	seen := make(map[string]bool)
	cursor := ""

	for page := range maxTodoistPages {
		targetURL := rawURL
		if page > 0 && cursor != "" {
			separator := "?"
			if strings.Contains(rawURL, "?") {
				separator = "&"
			}
			targetURL = fmt.Sprintf("%s%scursor=%s", rawURL, separator, url.QueryEscape(cursor))
		}

		body, err := a.doGet(ctx, targetURL)
		if err != nil {
			return nil, fmt.Errorf("failed to fetch todoist %s: %w", resourceLabel, err)
		}

		items, nextCursor, parseErr := unmarshalPageOrSlice[T](body)
		if parseErr != nil {
			return nil, fmt.Errorf("failed to parse todoist %s from %s: %w", resourceLabel, sanitizeURL(targetURL), parseErr)
		}
		all = append(all, items...)

		if nextCursor == "" || seen[nextCursor] {
			break
		}
		seen[nextCursor] = true
		cursor = nextCursor
	}

	return all, nil
}

func matchProjectIDByName(projects []todoistProject, target string) (string, string, bool) {
	for i := range projects {
		if strings.EqualFold(projects[i].Name, target) {
			return string(projects[i].ID), projects[i].Name, true
		}
	}
	return "", "", false
}

func (a *TodoistAdapter) resolveProjectID(ctx context.Context) (string, string, error) {
	if a.projectID != "" {
		projectURL := fmt.Sprintf("%s/api/v1/projects/%s", a.baseURL, url.PathEscape(a.projectID))
		body, err := a.doGet(ctx, projectURL)
		if err != nil {
			return "", "", err
		}
		var p todoistProject
		if err := json.Unmarshal(body, &p); err != nil {
			return "", "", fmt.Errorf("failed to parse todoist project from %s: %w", sanitizeURL(projectURL), err)
		}
		id := string(p.ID)
		if id == "" {
			id = a.projectID
		}
		return id, p.Name, nil
	}

	projectsURL := a.baseURL + "/api/v1/projects"
	projects, err := fetchPaginated[todoistProject](ctx, a, projectsURL, "projects")
	if err != nil {
		return "", "", err
	}

	if a.projectName != "" {
		if id, name, ok := matchProjectIDByName(projects, a.projectName); ok {
			return id, name, nil
		}
		return "", "", ErrUpstreamNotFound
	}

	for i := range projects {
		if projects[i].IsInbox() {
			return string(projects[i].ID), projects[i].Name, nil
		}
	}
	if len(projects) > 0 {
		return string(projects[0].ID), projects[0].Name, nil
	}
	return "", "Inbox", nil
}

func (a *TodoistAdapter) fetchProjectSections(ctx context.Context, projectID string) ([]string, map[string]string, error) {
	sections := []string{}
	sectionMap := make(map[string]string)
	if projectID == "" {
		return sections, sectionMap, nil
	}

	sectionsURL := fmt.Sprintf("%s/api/v1/sections?project_id=%s", a.baseURL, url.QueryEscape(projectID))
	rawSections, secErr := fetchPaginated[todoistSection](ctx, a, sectionsURL, "sections")
	if secErr != nil {
		return nil, nil, secErr
	}
	sort.SliceStable(rawSections, func(i, j int) bool { return rawSections[i].Order < rawSections[j].Order })
	sections = make([]string, 0, len(rawSections))
	for _, s := range rawSections {
		sections = append(sections, s.Name)
		if string(s.ID) != "" {
			sectionMap[string(s.ID)] = s.Name
		}
	}
	return sections, sectionMap, nil
}

func (a *TodoistAdapter) fetchCompletedProjectItems(ctx context.Context, projectID, listID string, sectionMap map[string]string) []tasks.ListItem {
	if projectID == "" {
		return nil
	}
	completedURL := fmt.Sprintf("%s/api/v1/tasks/completed?project_id=%s&limit=30", a.baseURL, url.QueryEscape(projectID))
	completedBody, cErr := a.doGet(ctx, completedURL)
	if cErr != nil {
		return nil
	}
	var completedResp todoistCompletedResponse
	if jsonErr := json.Unmarshal(completedBody, &completedResp); jsonErr != nil {
		return nil
	}
	var completedTasks []tasks.ListItem
	for _, item := range completedResp.Items {
		itemID := string(item.TaskID)
		if itemID == "" {
			itemID = string(item.ID)
		}
		if itemID == "" {
			continue
		}
		completedDateStr := item.CompletedAt
		if completedDateStr == "" {
			completedDateStr = item.CompletedDate
		}
		completedAt := parseTimeFlexible(completedDateStr)
		var section string
		if item.SectionID != "" {
			if name, ok := sectionMap[string(item.SectionID)]; ok {
				section = name
			} else {
				section = string(item.SectionID)
			}
		}
		completedTasks = append(completedTasks, tasks.ListItem{
			ID: itemID, ListID: listID, Title: item.Content, Done: true,
			Section: section, CreatedAt: completedAt, UpdatedAt: completedAt,
		})
	}
	return completedTasks
}

func convertTodoistTask(t todoistTask, listID string, sectionMap map[string]string, pos int) tasks.ListItem {
	id := string(t.ID)
	var section string
	if t.SectionID != "" {
		if name, ok := sectionMap[string(t.SectionID)]; ok {
			section = name
		} else {
			section = string(t.SectionID)
		}
	}
	var dueDate *string
	if t.Due != nil {
		raw := t.Due.Date
		if len(raw) < 10 {
			raw = t.Due.Datetime
		}
		if len(raw) >= 10 {
			if _, parseErr := time.Parse("2006-01-02", raw[:10]); parseErr == nil {
				s := raw[:10]
				dueDate = &s
			}
		}
	}
	createdAt := parseTimeFlexible(t.EffectiveCreatedAt())
	return tasks.ListItem{
		ID:        id,
		ListID:    listID,
		Title:     t.Content,
		Done:      t.EffectiveDone(),
		Section:   section,
		Position:  pos,
		DueDate:   dueDate,
		CreatedAt: createdAt,
		UpdatedAt: createdAt,
	}
}

// FetchList retrieves task list metadata and items from the Todoist API.
func (a *TodoistAdapter) FetchList(ctx context.Context) (*tasks.List, []tasks.ListItem, error) {
	resolvedProjectID, resolvedProjectTitle, err := a.resolveProjectID(ctx)
	if err != nil {
		return nil, nil, err
	}

	listTitle, listID := a.listName, resolvedProjectID
	if listTitle == "" {
		listTitle = resolvedProjectTitle
	}
	if listTitle == "" {
		listTitle = a.projectID
	}
	if listTitle == "" {
		listTitle = "Inbox"
	}
	if listID == "" {
		listID = a.projectID
	}
	if listID == "" {
		listID = "inbox"
	}

	sections, sectionMap, secErr := a.fetchProjectSections(ctx, resolvedProjectID)
	if secErr != nil {
		return nil, nil, secErr
	}

	var tasksURL string
	if resolvedProjectID != "" {
		tasksURL = fmt.Sprintf("%s/api/v1/tasks?project_id=%s", a.baseURL, url.QueryEscape(resolvedProjectID))
	} else {
		tasksURL = a.baseURL + "/api/v1/tasks"
	}
	rawTasks, taskErr := fetchPaginated[todoistTask](ctx, a, tasksURL, "tasks")
	if taskErr != nil {
		return nil, nil, taskErr
	}

	completedTasks := a.fetchCompletedProjectItems(ctx, resolvedProjectID, listID, sectionMap)

	orderedTasks := orderTodoistTasks(rawTasks)
	now := time.Now().UTC()
	items := make([]tasks.ListItem, 0, len(orderedTasks)+len(completedTasks))
	seenIDs := make(map[string]bool, len(orderedTasks)+len(completedTasks))

	for _, t := range orderedTasks {
		id := string(t.ID)
		if id == "" {
			continue
		}
		seenIDs[id] = true
		items = append(items, convertTodoistTask(t, listID, sectionMap, len(items)))
	}

	for _, ct := range completedTasks {
		if !seenIDs[ct.ID] {
			ct.Position = len(items)
			items = append(items, ct)
			seenIDs[ct.ID] = true
		}
	}

	return &tasks.List{
		ID:        listID,
		Name:      listTitle,
		Source:    "todoist",
		Sections:  sections,
		UpdatedAt: now,
	}, items, nil
}

func orderTodoistTasks(items []todoistTask) []todoistTask {
	if len(items) <= 1 {
		return items
	}
	itemByID := make(map[string]todoistTask, len(items))
	childrenByParent := make(map[string][]todoistTask)
	var topLevel []todoistTask

	for _, item := range items {
		itemByID[string(item.ID)] = item
	}
	for _, item := range items {
		parentID := string(item.ParentID)
		if parentID == "" {
			topLevel = append(topLevel, item)
		} else if _, exists := itemByID[parentID]; exists {
			childrenByParent[parentID] = append(childrenByParent[parentID], item)
		} else {
			topLevel = append(topLevel, item)
		}
	}

	sortTasks := func(slice []todoistTask) {
		sort.SliceStable(slice, func(i, j int) bool { return slice[i].EffectiveOrder() < slice[j].EffectiveOrder() })
	}
	sortTasks(topLevel)
	for parentID := range childrenByParent {
		sortTasks(childrenByParent[parentID])
	}

	ordered := make([]todoistTask, 0, len(items))
	visited := make(map[string]bool, len(items))

	var traverse func(item todoistTask, parentSectionID string)
	traverse = func(item todoistTask, parentSectionID string) {
		id := string(item.ID)
		if id == "" || visited[id] {
			return
		}
		visited[id] = true
		if item.SectionID == "" && parentSectionID != "" {
			item.SectionID = flexibleString(parentSectionID)
		}
		ordered = append(ordered, item)
		for _, child := range childrenByParent[id] {
			traverse(child, string(item.SectionID))
		}
	}
	for _, top := range topLevel {
		traverse(top, "")
	}
	for _, item := range items {
		if id := string(item.ID); id != "" && !visited[id] {
			ordered = append(ordered, item)
			visited[id] = true
		}
	}
	return ordered
}

func (a *TodoistAdapter) doGet(ctx context.Context, targetURL string) ([]byte, error) {
	b, _, err := doBearerGet(ctx, a.client, targetURL, a.token, "todoist request")
	return b, err
}

func parseTimeFlexible(raw string) time.Time {
	if raw == "" {
		return time.Now().UTC()
	}
	for _, format := range []string{time.RFC3339Nano, time.RFC3339, "2006-01-02 15:04:05", "2006-01-02"} {
		if t, err := time.Parse(format, raw); err == nil {
			return t.UTC()
		}
	}
	return time.Now().UTC()
}

// CompleteItem closes a task in Todoist via POST /api/v1/tasks/{itemID}/close.
func (a *TodoistAdapter) CompleteItem(ctx context.Context, itemID string) error {
	itemID = strings.TrimSpace(itemID)
	if itemID == "" {
		return errors.New("item ID is required")
	}
	if strings.Contains(itemID, "/") || strings.Contains(itemID, "..") {
		return errors.New("invalid item ID")
	}

	targetURL := fmt.Sprintf("%s/api/v1/tasks/%s/close", a.baseURL, url.PathEscape(itemID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, nil)
	if err != nil {
		return fmt.Errorf("failed to create http request: %w", err)
	}

	req.Header.Set("Accept", "application/json")
	if a.token != "" {
		req.Header.Set("Authorization", "Bearer "+a.token)
	}

	resp, err := a.client.Do(req)
	if err != nil {
		return fmt.Errorf("todoist close request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if err != nil {
		return fmt.Errorf("failed to drain response body: %w", err)
	}
	return mapUpstreamStatus(resp.StatusCode, targetURL)
}
