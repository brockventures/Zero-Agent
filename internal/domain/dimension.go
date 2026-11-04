package domain

import (
	"encoding/json"
	"fmt"

	"gopkg.in/yaml.v3"
)

// Dimension represents the grid footprint of a widget [cols, rows] on the 6x2 or 3x6 grid.
type Dimension struct {
	Cols int `yaml:"cols"`
	Rows int `yaml:"rows"`
}

// NewDimension creates a Dimension with the specified columns and rows.
func NewDimension(cols, rows int) Dimension {
	return Dimension{Cols: cols, Rows: rows}
}

// DimensionFromSlice converts a 2-element integer slice [cols, rows] to Dimension.
func DimensionFromSlice(s []int) (Dimension, error) {
	if len(s) != 2 {
		return Dimension{}, fmt.Errorf("dimension slice must contain exactly 2 integers [cols, rows], got %d", len(s))
	}
	d := Dimension{Cols: s[0], Rows: s[1]}
	if !d.IsValid() {
		return Dimension{}, fmt.Errorf("dimension [%d, %d] violates 6x2 / 3x6 grid bounds (cols 1..6, rows 1..2 or cols 1..3, rows 1..6)", d.Cols, d.Rows)
	}
	return d, nil
}

// Area returns the total number of discrete cells occupied by the dimension.
func (d Dimension) Area() int {
	return d.Cols * d.Rows
}

// IsValid checks if the dimension adheres to either the 6x2 landscape or 3x6 portrait grid bounds.
func (d Dimension) IsValid() bool {
	return d.IsValidForGrid(6, 2) || d.IsValidForGrid(3, 6)
}

// IsValidForGrid checks if the dimension fits within a gridCols x gridRows grid.
func (d Dimension) IsValidForGrid(gridCols, gridRows int) bool {
	return d.Cols >= 1 && d.Cols <= gridCols && d.Rows >= 1 && d.Rows <= gridRows
}

// Slice returns the dimension as a 2-element slice [cols, rows].
func (d Dimension) Slice() []int {
	return []int{d.Cols, d.Rows}
}

// String returns the string representation [cols, rows].
func (d Dimension) String() string {
	return fmt.Sprintf("[%d, %d]", d.Cols, d.Rows)
}

// UnmarshalYAML implements custom unmarshaling supporting both [cols, rows] sequences and mappings.
func (d *Dimension) UnmarshalYAML(node *yaml.Node) error {
	switch node.Kind {
	case yaml.SequenceNode:
		if len(node.Content) != 2 {
			return fmt.Errorf("line %d: dimensions sequence must contain exactly 2 integers [cols, rows], got %d", node.Line, len(node.Content))
		}
		var cols, rows int
		if err := node.Content[0].Decode(&cols); err != nil {
			return fmt.Errorf("line %d: invalid columns in dimension: %w", node.Content[0].Line, err)
		}
		if err := node.Content[1].Decode(&rows); err != nil {
			return fmt.Errorf("line %d: invalid rows in dimension: %w", node.Content[1].Line, err)
		}
		d.Cols = cols
		d.Rows = rows
		return nil
	case yaml.MappingNode:
		type rawDimension struct {
			Cols int `yaml:"cols"`
			Rows int `yaml:"rows"`
		}
		var raw rawDimension
		if err := node.Decode(&raw); err != nil {
			return err
		}
		d.Cols = raw.Cols
		d.Rows = raw.Rows
		return nil
	default:
		return fmt.Errorf("line %d: dimension must be sequence [cols, rows] or mapping", node.Line)
	}
}

// MarshalJSON serializes Dimension as a standard 2-element JSON array [cols, rows].
func (d Dimension) MarshalJSON() ([]byte, error) {
	return json.Marshal([]int{d.Cols, d.Rows})
}

// UnmarshalJSON deserializes Dimension from both a 2-element array [cols, rows] and an object {"cols": c, "rows": r}.
func (d *Dimension) UnmarshalJSON(data []byte) error {
	var slice []int
	if err := json.Unmarshal(data, &slice); err == nil {
		if len(slice) != 2 {
			return fmt.Errorf("dimension JSON array must contain exactly 2 integers [cols, rows], got %d", len(slice))
		}
		d.Cols = slice[0]
		d.Rows = slice[1]
		return nil
	}

	type rawDimension struct {
		Cols int `json:"cols"`
		Rows int `json:"rows"`
	}
	var raw rawDimension
	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("dimension must be JSON array [cols, rows] or object: %w", err)
	}
	d.Cols = raw.Cols
	d.Rows = raw.Rows
	return nil
}
