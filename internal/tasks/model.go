package tasks

import (
	"cmp"
	"errors"
	"slices"
	"time"
)

// Standard error definitions for task and list operations.
var (
	ErrListNotFound          = errors.New("list not found")
	ErrItemNotFound          = errors.New("item not found")
	ErrCompletionUnsupported = errors.New("task completion not supported by provider")
)

// List represents a distinct task or checklist entity Model.
type List struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Source    string    `json:"source"`
	Sections  []string  `json:"sections"`
	CreatedAt time.Time `json:"created_at,omitempty"`
	UpdatedAt time.Time `json:"updated_at"`
}

// ListItem represents an individual item or task belonging to a List Model.
type ListItem struct {
	ID        string    `json:"id"`
	ListID    string    `json:"list_id"`
	Title     string    `json:"title"`
	Done      bool      `json:"done"`
	Section   string    `json:"section,omitempty"`
	Position  int       `json:"position"`
	Assignee  *string   `json:"assignee"`
	DueDate   *string   `json:"due_date"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// TasksSnapshot represents the normalized payload published over the SSE bus (widget.update)
// and consumed by widgets Protocol.
type TasksSnapshot struct {
	List  List       `json:"list"`
	Items []ListItem `json:"items"`
}

// BuildSnapshot constructs a sorted, filtered TasksSnapshot from an upstream List and its ListItems.
func BuildSnapshot(list List, items []ListItem, showCompleted int) *TasksSnapshot {
	if list.Sections == nil {
		list.Sections = []string{}
	} else {
		list.Sections = slices.Clone(list.Sections)
	}

	active := make([]ListItem, 0, len(items))
	var completed []ListItem
	for _, itm := range items {
		if itm.Done {
			completed = append(completed, itm)
		} else {
			active = append(active, itm)
		}
	}

	slices.SortFunc(active, func(a, b ListItem) int {
		if c := cmp.Compare(a.Position, b.Position); c != 0 {
			return c
		}
		if c := a.CreatedAt.Compare(b.CreatedAt); c != 0 {
			return c
		}
		return cmp.Compare(a.ID, b.ID)
	})

	if showCompleted != 0 && len(completed) > 0 {
		slices.SortFunc(completed, func(a, b ListItem) int {
			if c := b.UpdatedAt.Compare(a.UpdatedAt); c != 0 {
				return c
			}
			if c := cmp.Compare(a.Position, b.Position); c != 0 {
				return c
			}
			return cmp.Compare(a.ID, b.ID)
		})
		if showCompleted > 0 && len(completed) > showCompleted {
			completed = completed[:showCompleted]
		}
		active = append(active, completed...)
	}

	return &TasksSnapshot{
		List:  list,
		Items: active,
	}
}
