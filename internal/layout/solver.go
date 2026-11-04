package layout

import (
	"fmt"
	"math/bits"
	"slices"
	"sort"
	"strings"

	"github.com/azylman/mirrormere/internal/domain"
)

const (
	// GridColumns is the fixed horizontal cell count of the Mirrormere canvas.
	GridColumns = 6

	// GridRows is the fixed vertical cell count of the Mirrormere canvas below the header.
	GridRows = 2

	// GridTotalCells is the total discrete cells on a single 6x2 grid screen.
	GridTotalCells = GridColumns * GridRows

	// FullGridMask is the 12-bit mask representing 100% cell coverage (0x0FFF = 4095).
	FullGridMask uint32 = (1 << GridTotalCells) - 1
)

func fullGridMask(gridCols, gridRows int) uint32 {
	return (uint32(1) << (gridCols * gridRows)) - 1
}

// WidgetInput represents a declared widget instance to be packed by the 6x2 layout solver.
type WidgetInput struct {
	ID         string           `json:"id"`
	Type       string           `json:"type,omitempty"`
	Dimensions domain.Dimension `json:"dimensions"`
	Pinned     bool             `json:"pinned,omitempty"`
}

// ExplicitPlacement represents a user-positioned widget on a specific screen and coordinate origin.
type ExplicitPlacement struct {
	ID         string           `json:"id"`
	Type       string           `json:"type,omitempty"`
	Screen     int              `json:"screen"`
	Origin     [2]int           `json:"origin"`
	Dimensions domain.Dimension `json:"dimensions"`
	Pinned     bool             `json:"pinned,omitempty"`
}

// PlacedWidget represents a placed widget instance with its origin and dimensions on the 6x2 grid.
type PlacedWidget struct {
	WidgetID   string           `json:"widget_id"`
	Type       string           `json:"type,omitempty"`
	Origin     [2]int           `json:"origin"`     // [col, row]
	Dimensions domain.Dimension `json:"dimensions"` // [cols, rows]
	Pinned     bool             `json:"pinned,omitempty"`
}

func (pw PlacedWidget) Col() int  { return pw.Origin[0] }
func (pw PlacedWidget) Row() int  { return pw.Origin[1] }
func (pw PlacedWidget) Cols() int { return pw.Dimensions.Cols }
func (pw PlacedWidget) Rows() int { return pw.Dimensions.Rows }

// Screen represents one 6x2 grid screen containing a set of non-overlapping, fully tiled widgets.
type Screen struct {
	Index   int            `json:"index"`
	Bitmask uint32         `json:"bitmask"`
	Widgets []PlacedWidget `json:"widgets"`
}

// Layout encapsulates the complete solved rotation layout across all computed screens.
type Layout struct {
	TotalScreens int      `json:"total_screens"`
	Screens      []Screen `json:"screens"`
}

// ComputeWidgetBitmask computes the bitmask assuming bounds have already been validated on the default 6x2 grid.
func ComputeWidgetBitmask(cols, rows, originCol, originRow int) uint32 {
	rowMask := uint32(((1 << cols) - 1) << originCol)
	if rows == 1 {
		return rowMask << (originRow * GridColumns)
	}
	return rowMask | (rowMask << GridColumns)
}

// ComputeWidgetBitmaskForGrid computes the bitmask for a widget of size (cols, rows) at (originCol, originRow) on a grid of width gridCols.
func ComputeWidgetBitmaskForGrid(cols, rows, originCol, originRow, gridCols int) uint32 {
	if gridCols == GridColumns && rows <= 2 {
		return ComputeWidgetBitmask(cols, rows, originCol, originRow)
	}
	rowMask := uint32(((1 << cols) - 1) << originCol)
	var mask uint32
	for r := range rows {
		mask |= rowMask << ((originRow + r) * gridCols)
	}
	return mask
}

// NormalizeGrid validates and normalizes grid dimensions to either 6x2, 3x6, or 2x6.
func NormalizeGrid(gridCols, gridRows int) (int, int) {
	if gridCols == 3 && gridRows == 6 {
		return 3, 6
	}
	if gridCols == 2 && gridRows == 6 {
		return 2, 6
	}
	return GridColumns, GridRows
}

// ComputeScreens calculates the minimal number of rotation screens K and cell deficit.
func ComputeScreens(pinnedArea, unpinnedArea int) (int, int, error) {
	return ComputeScreensForGrid(pinnedArea, unpinnedArea, GridTotalCells)
}

// ComputeScreensForGrid calculates the minimal number of rotation screens K and cell deficit for totalCells per screen.
func ComputeScreensForGrid(pinnedArea, unpinnedArea, totalCells int) (int, int, error) {
	if pinnedArea < 0 || unpinnedArea < 0 {
		return 0, 0, fmt.Errorf("widget areas must be non-negative: pinned=%d, unpinned=%d", pinnedArea, unpinnedArea)
	}
	if pinnedArea > totalCells {
		return 0, 0, fmt.Errorf("[Mirrormere Config Error] Invalid widget layout configuration:\n  Pinned widgets occupy %d cells, exceeding single-screen limit of %d cells.", pinnedArea, totalCells)
	}
	if pinnedArea == totalCells {
		if unpinnedArea > 0 {
			return 0, 0, fmt.Errorf("[Mirrormere Config Error] Invalid widget layout configuration:\n  Pinned widgets occupy all %d cells, leaving no screen space for %d unpinned cells.", totalCells, unpinnedArea)
		}
		return 1, 0, nil
	}

	capacityPerScreen := totalCells - pinnedArea
	if unpinnedArea == 0 {
		return 1, capacityPerScreen, nil
	}

	k := (unpinnedArea + capacityPerScreen - 1) / capacityPerScreen
	return k, (k * capacityPerScreen) - unpinnedArea, nil
}

// FormatDeficitError formats layout configuration errors Load Diagnostics & Deficit Guidance.
func FormatDeficitError(totalArea, targetScreens, deficit int) error {
	return formatGridDeficitError(totalArea, targetScreens, deficit, GridColumns, GridRows)
}

func formatDeficitErrorForGrid(totalArea, targetScreens, deficit, gridCols, gridRows int) error {
	if gridCols == GridColumns && gridRows == GridRows {
		return FormatDeficitError(totalArea, targetScreens, deficit)
	}
	return formatGridDeficitError(totalArea, targetScreens, deficit, gridCols, gridRows)
}

func formatGridDeficitError(totalArea, targetScreens, deficit, gridCols, gridRows int) error {
	totalCells := gridCols * gridRows
	portrait := (gridCols == 2 || gridCols == 3) && gridRows == 6
	var b strings.Builder
	fmt.Fprintf(&b, "[Mirrormere Config Error] Invalid widget layout configuration:\n  Total widget area is %d cells.\n  A %dx%d grid requires screen areas in multiples of %d (Target: %d screens = %d cells).\n  Deficit: %d cells needed to achieve 100%% fully-filled screens.\n  Suggestions:\n", totalArea, gridCols, gridRows, totalCells, targetScreens, targetScreens*totalCells, deficit)
	for _, s := range buildSuggestions(deficit, portrait) {
		fmt.Fprintf(&b, "    - %s\n", s)
	}
	return fmt.Errorf("%s", strings.TrimRight(b.String(), "\n"))
}

func buildSuggestions(deficit int, portrait bool) []string {
	sp := recommendedSpacerDimensions(deficit, portrait)
	expandHint := "Or expand existing [2, 1] widgets to [3, 2] / [4, 2]."
	if portrait {
		expandHint = "Or expand existing [1, 2] widgets to [2, 3] / [2, 4]."
	}
	sugs := []string{
		expandHint,
		fmt.Sprintf("Or insert native spacer tiles (e.g. { type: \"spacer\", dimensions: [%d, %d] }) to intentionally leave layout space open.", sp[0], sp[1]),
	}
	if add := decomposeDeficit(deficit, portrait); add != "" {
		return append([]string{add}, sugs...)
	}
	return sugs
}

func decomposeDeficit(deficit int, portrait bool) string {
	if deficit == 18 && portrait {
		return "Add a [3, 6] widget (18 cells)."
	}
	if deficit == 12 {
		if portrait {
			return "Add a [2, 6] widget (12 cells)."
		}
		return "Add a [6, 2] widget (12 cells)."
	}
	var parts []string
	for _, opt := range []struct {
		dim  [2]int
		area int
	}{
		{[2]int{4, 2}, 8},
		{[2]int{3, 2}, 6},
		{[2]int{2, 2}, 4},
		{[2]int{3, 1}, 3},
		{[2]int{2, 1}, 2},
		{[2]int{1, 1}, 1},
	} {
		dim := opt.dim
		if portrait {
			dim = [2]int{opt.dim[1], opt.dim[0]}
		}
		for deficit >= opt.area && opt.area > 0 {
			parts = append(parts, fmt.Sprintf("a [%d, %d] widget (%d cells)", dim[0], dim[1], opt.area))
			deficit -= opt.area
		}
	}
	switch len(parts) {
	case 0:
		return ""
	case 1:
		return "Add " + parts[0] + "."
	case 2:
		return "Add " + parts[0] + " and " + parts[1] + "."
	default:
		return "Add " + strings.Join(parts[:len(parts)-1], ", ") + ", and " + parts[len(parts)-1] + "."
	}
}

func recommendedSpacerDimensions(deficit int, portrait bool) [2]int {
	var dim [2]int
	switch deficit {
	case 1, 2, 3:
		dim = [2]int{deficit, 1}
	case 4, 10:
		dim = [2]int{2, 2}
	case 6:
		dim = [2]int{3, 2}
	case 8:
		dim = [2]int{4, 2}
	case 12:
		dim = [2]int{6, 2}
	case 18:
		dim = [2]int{6, 3}
	default:
		if deficit <= 6 {
			dim = [2]int{deficit, 1}
		} else {
			dim = [2]int{2, 1}
		}
	}
	if portrait {
		return [2]int{dim[1], dim[0]}
	}
	return dim
}

const defaultMaxSearchNodes = 100_000

type shapeGroup struct {
	dims    domain.Dimension
	count   int
	widgets []WidgetInput
	orders  []int
}

func (sg *shapeGroup) nextOrder() int {
	idx := len(sg.widgets) - sg.count
	if idx >= 0 && idx < len(sg.orders) {
		return sg.orders[idx]
	}
	return 1 << 30
}

type unpinnedPlacement struct {
	screen int
	origin [2]int
	widget WidgetInput
	order  int
}

// Solve executes exact 2D recursive backtracking bitmask bin-packing for the default 6x2 grid.
func Solve(widgets []WidgetInput) (*Layout, error) {
	return SolveWithBudget(widgets, defaultMaxSearchNodes)
}

// SolveWithBudget executes the solver with an explicit search node ceiling on the default 6x2 grid.
func SolveWithBudget(widgets []WidgetInput, maxSearchNodes int) (*Layout, error) {
	return SolveGridWithBudget(widgets, GridColumns, GridRows, maxSearchNodes)
}

func partitionAndValidateWidgets(widgets []WidgetInput, gridCols, gridRows int) ([]WidgetInput, []WidgetInput, int, error) {
	seenIDs := make(map[string]int, len(widgets))
	var pinned, unpinned []WidgetInput
	var pinnedArea, unpinnedArea int
	totalCells := gridCols * gridRows

	for i, w := range widgets {
		trimmedID := strings.TrimSpace(w.ID)
		if trimmedID == "" {
			return nil, nil, 0, fmt.Errorf("widget at index %d has empty id", i)
		}
		if prevIdx, exists := seenIDs[trimmedID]; exists {
			return nil, nil, 0, fmt.Errorf("duplicate widget id '%s' found (indices %d and %d)", trimmedID, prevIdx, i)
		}
		seenIDs[trimmedID] = i

		if !w.Dimensions.IsValidForGrid(gridCols, gridRows) {
			return nil, nil, 0, fmt.Errorf("widget '%s' has invalid dimensions [%d, %d]: cols must be 1..%d, rows must be 1..%d", w.ID, w.Dimensions.Cols, w.Dimensions.Rows, gridCols, gridRows)
		}

		area := w.Dimensions.Area()
		if w.Pinned {
			pinned = append(pinned, w)
			pinnedArea += area
		} else {
			unpinned = append(unpinned, w)
			unpinnedArea += area
		}
	}

	var (
		k, deficit int
		err        error
	)
	if totalCells == GridTotalCells {
		k, deficit, err = ComputeScreens(pinnedArea, unpinnedArea)
	} else {
		k, deficit, err = ComputeScreensForGrid(pinnedArea, unpinnedArea, totalCells)
	}
	if err != nil {
		return nil, nil, 0, err
	}
	if deficit > 0 {
		effectiveTotalArea := (k * totalCells) - deficit
		return nil, nil, 0, formatDeficitErrorForGrid(effectiveTotalArea, k, deficit, gridCols, gridRows)
	}
	return pinned, unpinned, k, nil
}

func sortAndGroupWidgets(pinned, unpinned []WidgetInput) []*shapeGroup {
	sort.SliceStable(pinned, func(i, j int) bool {
		if pinned[i].Dimensions.Area() != pinned[j].Dimensions.Area() {
			return pinned[i].Dimensions.Area() > pinned[j].Dimensions.Area()
		}
		if pinned[i].Dimensions.Rows != pinned[j].Dimensions.Rows {
			return pinned[i].Dimensions.Rows > pinned[j].Dimensions.Rows
		}
		return pinned[i].ID < pinned[j].ID
	})

	shapeMap := make(map[[2]int]*shapeGroup)
	var shapeGroups []*shapeGroup
	for idx, w := range unpinned {
		key := [2]int{w.Dimensions.Cols, w.Dimensions.Rows}
		sg, exists := shapeMap[key]
		if !exists {
			sg = &shapeGroup{dims: w.Dimensions}
			shapeMap[key] = sg
			shapeGroups = append(shapeGroups, sg)
		}
		sg.count++
		sg.widgets = append(sg.widgets, w)
		sg.orders = append(sg.orders, idx)
	}

	sort.Slice(shapeGroups, func(i, j int) bool {
		if shapeGroups[i].dims.Area() != shapeGroups[j].dims.Area() {
			return shapeGroups[i].dims.Area() > shapeGroups[j].dims.Area()
		}
		return shapeGroups[i].dims.Rows > shapeGroups[j].dims.Rows
	})

	return shapeGroups
}

func assembleSolvedLayout(k int, screenMasks []uint32, pinnedPlacements []PlacedWidget, unpinnedPlacements []unpinnedPlacement, placedCount int) *Layout {
	res := &Layout{TotalScreens: k, Screens: make([]Screen, k)}
	minOrder := make([]int, k)
	for s := range k {
		minOrder[s] = 1 << 30
		screenWidgets := make([]PlacedWidget, 0, len(pinnedPlacements)+placedCount)
		screenWidgets = append(screenWidgets, pinnedPlacements...)
		for i := range placedCount {
			if unpinnedPlacements[i].screen == s {
				if unpinnedPlacements[i].order < minOrder[s] {
					minOrder[s] = unpinnedPlacements[i].order
				}
				screenWidgets = append(screenWidgets, PlacedWidget{
					WidgetID: unpinnedPlacements[i].widget.ID, Type: unpinnedPlacements[i].widget.Type,
					Origin: unpinnedPlacements[i].origin, Dimensions: unpinnedPlacements[i].widget.Dimensions,
				})
			}
		}
		sort.Slice(screenWidgets, func(i, j int) bool {
			if screenWidgets[i].Row() != screenWidgets[j].Row() {
				return screenWidgets[i].Row() < screenWidgets[j].Row()
			}
			return screenWidgets[i].Col() < screenWidgets[j].Col()
		})
		res.Screens[s] = Screen{Index: s, Bitmask: screenMasks[s], Widgets: screenWidgets}
	}
	orderIdx := make([]int, k)
	for s := range k {
		orderIdx[s] = s
	}
	sort.SliceStable(orderIdx, func(i, j int) bool {
		return minOrder[orderIdx[i]] < minOrder[orderIdx[j]]
	})
	sortedScreens := make([]Screen, k)
	for newIdx, oldIdx := range orderIdx {
		scr := res.Screens[oldIdx]
		scr.Index = newIdx
		sortedScreens[newIdx] = scr
	}
	res.Screens = sortedScreens
	return res
}

type gridSolver struct {
	gridCols           int
	gridRows           int
	maxSearchNodes     int
	k                  int
	pinned             []WidgetInput
	unpinned           []WidgetInput
	shapeGroups        []*shapeGroup
	screenMasks        []uint32
	pinnedPlacements   []PlacedWidget
	unpinnedPlacements []unpinnedPlacement
	placedCount        int
	nodeCount          int
	orderByConfig      bool
}

func (s *gridSolver) placePinned(pIdx int) bool {
	if pIdx == len(s.pinned) {
		if len(s.unpinned) == 0 {
			return true
		}
		origBudget := s.maxSearchNodes
		savedNodes := s.nodeCount
		seqCeiling := savedNodes + min(512, max(0, origBudget-savedNodes))
		s.orderByConfig = true
		s.maxSearchNodes = seqCeiling
		if s.placeUnpinnedExactCover() {
			s.maxSearchNodes = origBudget
			return true
		}
		hitSeqCeiling := s.nodeCount > seqCeiling
		s.orderByConfig = false
		s.maxSearchNodes = origBudget
		if !hitSeqCeiling || s.nodeCount >= origBudget {
			return false
		}
		return s.placeUnpinnedExactCover()
	}
	pw := s.pinned[pIdx]
	w, h := pw.Dimensions.Cols, pw.Dimensions.Rows
	maxCol, maxRow := s.gridCols-w, s.gridRows-h
	for r := 0; r <= maxRow; r++ {
		for c := 0; c <= maxCol; c++ {
			mask := ComputeWidgetBitmaskForGrid(w, h, c, r, s.gridCols)
			if (s.screenMasks[0] & mask) != 0 {
				continue
			}
			fillMaskAllScreens(s.screenMasks, s.k, mask, true)
			s.pinnedPlacements[pIdx] = PlacedWidget{
				WidgetID: pw.ID, Type: pw.Type, Origin: [2]int{c, r}, Dimensions: pw.Dimensions, Pinned: true,
			}
			if s.placePinned(pIdx + 1) {
				return true
			}
			fillMaskAllScreens(s.screenMasks, s.k, mask, false)
		}
	}
	return false
}

func (s *gridSolver) tryPlaceShapeGroup(sg *shapeGroup, targetScreen int, sMask uint32, c, r int) bool {
	if sg.count == 0 || c+sg.dims.Cols > s.gridCols || r+sg.dims.Rows > s.gridRows {
		return false
	}
	mask := ComputeWidgetBitmaskForGrid(sg.dims.Cols, sg.dims.Rows, c, r, s.gridCols)
	if (sMask & mask) != 0 {
		return false
	}
	s.screenMasks[targetScreen] |= mask
	wIdx := len(sg.widgets) - sg.count
	sg.count--
	s.unpinnedPlacements[s.placedCount] = unpinnedPlacement{
		screen: targetScreen, origin: [2]int{c, r}, widget: sg.widgets[wIdx], order: sg.orders[wIdx],
	}
	s.placedCount++
	if s.placeUnpinnedExactCover() {
		return true
	}
	s.placedCount--
	sg.count++
	s.screenMasks[targetScreen] &= ^mask
	return false
}

func (s *gridSolver) placeUnpinnedExactCover() bool {
	s.nodeCount++
	if s.nodeCount > s.maxSearchNodes {
		return false
	}
	targetScreen := firstIncompleteScreen(s.screenMasks, s.k, s.gridCols, s.gridRows)
	if targetScreen == -1 {
		return true
	}

	sMask := s.screenMasks[targetScreen]
	emptyCell := bits.TrailingZeros32(^sMask)
	c, r := emptyCell%s.gridCols, emptyCell/s.gridCols

	if s.orderByConfig {
		candidates := make([]*shapeGroup, 0, len(s.shapeGroups))
		for _, sg := range s.shapeGroups {
			if sg.count > 0 {
				candidates = append(candidates, sg)
			}
		}
		sort.Slice(candidates, func(i, j int) bool {
			return candidates[i].nextOrder() < candidates[j].nextOrder()
		})
		for _, sg := range candidates {
			if s.tryPlaceShapeGroup(sg, targetScreen, sMask, c, r) {
				return true
			}
		}
		return false
	}

	for _, sg := range s.shapeGroups {
		if s.tryPlaceShapeGroup(sg, targetScreen, sMask, c, r) {
			return true
		}
	}
	return false
}

// SolveGridWithBudget executes the solver for a 6x2 or 2x6 grid with an explicit search node ceiling.
func SolveGridWithBudget(widgets []WidgetInput, gridCols, gridRows, maxSearchNodes int) (*Layout, error) {
	gridCols, gridRows = NormalizeGrid(gridCols, gridRows)
	totalCells := gridCols * gridRows
	if maxSearchNodes <= 0 {
		maxSearchNodes = defaultMaxSearchNodes
	}
	if len(widgets) == 0 {
		return nil, formatDeficitErrorForGrid(0, 1, totalCells, gridCols, gridRows)
	}

	pinned, unpinned, k, err := partitionAndValidateWidgets(widgets, gridCols, gridRows)
	if err != nil {
		return nil, err
	}
	shapeGroups := sortAndGroupWidgets(pinned, unpinned)

	solver := &gridSolver{
		gridCols:           gridCols,
		gridRows:           gridRows,
		maxSearchNodes:     maxSearchNodes,
		k:                  k,
		pinned:             pinned,
		unpinned:           unpinned,
		shapeGroups:        shapeGroups,
		screenMasks:        make([]uint32, k),
		pinnedPlacements:   make([]PlacedWidget, len(pinned)),
		unpinnedPlacements: make([]unpinnedPlacement, len(unpinned)),
	}

	if !solver.placePinned(0) {
		return nil, fmt.Errorf("[Mirrormere Config Error] Invalid widget layout configuration:\n  Widgets satisfy total cell count but cannot geometrically tile the %dx%d grid without gaps or overlaps.\n  Suggestions:\n    - Adjust widget dimensions to fit the %dx%d grid.\n    - Insert native spacer tiles (e.g. { type: \"spacer\", dimensions: [2, 1] }) to pad irregular shapes.", gridCols, gridRows, gridCols, gridRows)
	}
	return assembleSolvedLayout(k, solver.screenMasks, solver.pinnedPlacements, solver.unpinnedPlacements, solver.placedCount), nil
}

func firstIncompleteScreen(screenMasks []uint32, k, gridCols, gridRows int) int {
	mask := fullGridMask(gridCols, gridRows)
	for s := range k {
		if screenMasks[s] != mask {
			return s
		}
	}
	return -1
}

func fillMaskAllScreens(screenMasks []uint32, k int, mask uint32, set bool) {
	for s := range k {
		if set {
			screenMasks[s] |= mask
		} else {
			screenMasks[s] &= ^mask
		}
	}
}

// SolveExplicit places widgets at explicit coordinates and greedily fills remaining cells with auto-spacers.
func SolveExplicit(placements []ExplicitPlacement, gridCols, gridRows int) (*Layout, []PlacedWidget, error) {
	gridCols, gridRows = NormalizeGrid(gridCols, gridRows)
	unpinnedScreens, err := validateExplicitPlacements(placements, gridCols, gridRows)
	if err != nil {
		return nil, nil, err
	}
	k, screenMap := compactScreenIndices(unpinnedScreens)
	screenMasks, screenWidgets, err := placeExplicitWidgets(placements, k, screenMap, gridCols)
	if err != nil {
		return nil, nil, err
	}
	var allSpacers []PlacedWidget
	screens := make([]Screen, k)
	for s := range k {
		spacers, sMask := fillRemainingWithSpacers(s, screenMasks[s], gridCols, gridRows)
		screenMasks[s] = sMask
		allSpacers = append(allSpacers, spacers...)
		screenWidgets[s] = append(screenWidgets[s], spacers...)
		sortPlacedWidgets(screenWidgets[s])
		screens[s] = Screen{
			Index:   s,
			Bitmask: screenMasks[s],
			Widgets: screenWidgets[s],
		}
	}
	return &Layout{TotalScreens: k, Screens: screens}, allSpacers, nil
}

func validateExplicitPlacements(placements []ExplicitPlacement, gridCols, gridRows int) ([]int, error) {
	seenIDs := make(map[string]int, len(placements))
	var unpinnedScreens []int
	for i, p := range placements {
		trimmedID := strings.TrimSpace(p.ID)
		if trimmedID == "" {
			return nil, fmt.Errorf("widget at index %d has empty id", i)
		}
		if prevIdx, exists := seenIDs[trimmedID]; exists {
			return nil, fmt.Errorf("duplicate widget id '%s' found (indices %d and %d)", trimmedID, prevIdx, i)
		}
		seenIDs[trimmedID] = i
		if !p.Dimensions.IsValidForGrid(gridCols, gridRows) {
			return nil, fmt.Errorf("widget '%s' has invalid dimensions [%d, %d]: cols must be 1..%d, rows must be 1..%d", p.ID, p.Dimensions.Cols, p.Dimensions.Rows, gridCols, gridRows)
		}
		col, row := p.Origin[0], p.Origin[1]
		cols, rows := p.Dimensions.Cols, p.Dimensions.Rows
		if col < 0 || col+cols > gridCols || row < 0 || row+rows > gridRows {
			return nil, fmt.Errorf("widget '%s' out of bounds at origin [%d, %d] with dimensions [%d, %d] for %dx%d grid", p.ID, col, row, cols, rows, gridCols, gridRows)
		}
		if p.Screen < 0 || p.Screen >= 16 {
			return nil, fmt.Errorf("widget '%s' has invalid screen %d: must be 0..15", p.ID, p.Screen)
		}
		if !p.Pinned {
			unpinnedScreens = append(unpinnedScreens, p.Screen)
		}
	}
	return unpinnedScreens, nil
}

func compactScreenIndices(unpinnedScreens []int) (int, map[int]int) {
	slices.Sort(unpinnedScreens)
	unpinnedScreens = slices.Compact(unpinnedScreens)
	count := len(unpinnedScreens)
	k := max(1, count)
	screenMap := make(map[int]int, count)
	for newIdx, oldIdx := range unpinnedScreens {
		screenMap[oldIdx] = newIdx
	}
	return k, screenMap
}

func placeExplicitWidgets(placements []ExplicitPlacement, k int, screenMap map[int]int, gridCols int) ([]uint32, [][]PlacedWidget, error) {
	screenMasks := make([]uint32, k)
	screenWidgets := make([][]PlacedWidget, k)
	for _, p := range placements {
		if !p.Pinned {
			continue
		}
		mask := ComputeWidgetBitmaskForGrid(p.Dimensions.Cols, p.Dimensions.Rows, p.Origin[0], p.Origin[1], gridCols)
		for s := range k {
			if (screenMasks[s] & mask) != 0 {
				return nil, nil, fmt.Errorf("widget overlap on screen %d at [%d, %d]", s, p.Origin[0], p.Origin[1])
			}
		}
		fillMaskAllScreens(screenMasks, k, mask, true)
		pw := PlacedWidget{
			WidgetID:   p.ID,
			Type:       p.Type,
			Origin:     p.Origin,
			Dimensions: p.Dimensions,
			Pinned:     true,
		}
		for s := range k {
			screenWidgets[s] = append(screenWidgets[s], pw)
		}
	}
	for _, p := range placements {
		if p.Pinned {
			continue
		}
		targetScreen := screenMap[p.Screen]
		mask := ComputeWidgetBitmaskForGrid(p.Dimensions.Cols, p.Dimensions.Rows, p.Origin[0], p.Origin[1], gridCols)
		if (screenMasks[targetScreen] & mask) != 0 {
			return nil, nil, fmt.Errorf("widget overlap on screen %d at [%d, %d]", targetScreen, p.Origin[0], p.Origin[1])
		}
		screenMasks[targetScreen] |= mask
		screenWidgets[targetScreen] = append(screenWidgets[targetScreen], PlacedWidget{
			WidgetID:   p.ID,
			Type:       p.Type,
			Origin:     p.Origin,
			Dimensions: p.Dimensions,
			Pinned:     false,
		})
	}
	return screenMasks, screenWidgets, nil
}

func isFree(sMask uint32, c, r, w, h, gridCols int) bool {
	mask := ComputeWidgetBitmaskForGrid(w, h, c, r, gridCols)
	return (sMask & mask) == 0
}

func isBetterSpacer(w, h int, current domain.Dimension) bool {
	area := w * h
	curArea := current.Area()
	return area > curArea || (area == curArea && h > current.Rows)
}

func largestSpacerAt(sMask uint32, c, r, gridCols, gridRows int) domain.Dimension {
	var best domain.Dimension
	for h := gridRows - r; h >= 1; h-- {
		for w := gridCols - c; w >= 1; w-- {
			if isFree(sMask, c, r, w, h, gridCols) && isBetterSpacer(w, h, best) {
				best = domain.Dimension{Cols: w, Rows: h}
			}
		}
	}
	return best
}

func fillRemainingWithSpacers(s int, sMask uint32, gridCols, gridRows int) ([]PlacedWidget, uint32) {
	var spacers []PlacedWidget
	fullMask := fullGridMask(gridCols, gridRows)
	for sMask != fullMask {
		emptyCell := bits.TrailingZeros32(^sMask)
		c, r := emptyCell%gridCols, emptyCell/gridCols
		dim := largestSpacerAt(sMask, c, r, gridCols, gridRows)
		mask := ComputeWidgetBitmaskForGrid(dim.Cols, dim.Rows, c, r, gridCols)
		sMask |= mask
		spacers = append(spacers, PlacedWidget{
			WidgetID:   fmt.Sprintf("auto-spacer-%d-%d-%d", s, c, r),
			Type:       "spacer",
			Origin:     [2]int{c, r},
			Dimensions: dim,
		})
	}
	return spacers, sMask
}

func sortPlacedWidgets(widgets []PlacedWidget) {
	sort.Slice(widgets, func(i, j int) bool {
		if widgets[i].Row() != widgets[j].Row() {
			return widgets[i].Row() < widgets[j].Row()
		}
		return widgets[i].Col() < widgets[j].Col()
	})
}
