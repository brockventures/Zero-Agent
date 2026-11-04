package tasks_test

import (
	"reflect"
	"testing"
	"time"

	"github.com/azylman/mirrormere/internal/tasks"
)

func mkItem(id string, done bool, pos int, createdMin, updatedMin int) tasks.ListItem {
	base := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	return tasks.ListItem{
		ID:        id,
		ListID:    "chores",
		Title:     "Task " + id,
		Done:      done,
		Position:  pos,
		CreatedAt: base.Add(time.Duration(createdMin) * time.Minute),
		UpdatedAt: base.Add(time.Duration(updatedMin) * time.Minute),
	}
}

func TestBuildSnapshot(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name          string
		list          tasks.List
		items         []tasks.ListItem
		showCompleted int
		wantIDs       []string
		wantSections  []string
	}{
		{
			name:          "empty_items_nil_sections",
			list:          tasks.List{ID: "chores", Name: "Chores"},
			items:         nil,
			showCompleted: 3,
			wantIDs:       []string{},
			wantSections:  []string{},
		},
		{
			name:          "active_sorting_and_completed_hidden",
			list:          tasks.List{ID: "chores", Name: "Chores", Sections: []string{"Kitchen"}},
			items:         []tasks.ListItem{mkItem("b", false, 2, 1, 1), mkItem("a2", false, 1, 5, 5), mkItem("a1", false, 1, 2, 2), mkItem("a0", false, 1, 2, 2), mkItem("d1", true, 0, 1, 10)},
			showCompleted: 0,
			wantIDs:       []string{"a0", "a1", "a2", "b"},
			wantSections:  []string{"Kitchen"},
		},
		{
			name:          "completed_sorted_and_capped",
			list:          tasks.List{ID: "chores", Name: "Chores"},
			items:         []tasks.ListItem{mkItem("act", false, 1, 1, 1), mkItem("d-old", true, 1, 1, 5), mkItem("d-tie-b", true, 2, 1, 20), mkItem("d-tie-a", true, 1, 1, 20), mkItem("d-id-b", true, 1, 1, 30), mkItem("d-id-a", true, 1, 1, 30)},
			showCompleted: 3,
			wantIDs:       []string{"act", "d-id-a", "d-id-b", "d-tie-a"},
			wantSections:  []string{},
		},
		{
			name:          "negative_show_completed_includes_all_done",
			list:          tasks.List{ID: "chores", Name: "Chores"},
			items:         []tasks.ListItem{mkItem("d1", true, 2, 1, 10), mkItem("d2", true, 1, 1, 10)},
			showCompleted: -1,
			wantIDs:       []string{"d2", "d1"},
			wantSections:  []string{},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			snap := tasks.BuildSnapshot(tc.list, tc.items, tc.showCompleted)
			if !reflect.DeepEqual(snap.List.Sections, tc.wantSections) {
				t.Fatalf("Sections = %v, want %v", snap.List.Sections, tc.wantSections)
			}
			gotIDs := make([]string, len(snap.Items))
			for i, itm := range snap.Items {
				gotIDs[i] = itm.ID
			}
			if !reflect.DeepEqual(gotIDs, tc.wantIDs) {
				t.Fatalf("item IDs = %v, want %v", gotIDs, tc.wantIDs)
			}
		})
	}
}
