package provider

import "github.com/azylman/mirrormere/internal/tasks/adapters"

// NewTasksProviderWithAdapter constructs a TasksProvider with an injected Adapter for testing.
func NewTasksProviderWithAdapter(adapter adapters.Adapter) *TasksProvider {
	return &TasksProvider{
		adapter: adapter,
	}
}
