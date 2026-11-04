package covflush

import (
	"archive/tar"
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

type failWriter struct {
	err error
}

func (fw failWriter) Write(p []byte) (n int, err error) {
	return 0, fw.err
}

var mu sync.Mutex

func TestWriteArchive(t *testing.T) {
	tests := []struct {
		name         string
		failTempDir  bool
		failReadDir  bool
		failRemove   bool
		metaWriteErr bool
		countersErr  bool
		failWriter   bool
		wantFiles    int
		wantErr      bool
	}{
		{
			name:      "success with files",
			wantFiles: 2,
			wantErr:   false,
		},
		{
			name:         "metaFn error ignored",
			metaWriteErr: true,
			wantFiles:    1,
			wantErr:      false,
		},
		{
			name:        "countersFn returns error",
			countersErr: true,
			wantErr:     true,
		},
		{
			name:       "writer failure on tar write",
			failWriter: true,
			wantErr:    true,
		},
		{
			name:        "mkdirTemp failure",
			failTempDir: true,
			wantErr:     true,
		},
		{
			name:        "readDir failure",
			failReadDir: true,
			wantErr:     true,
		},
		{
			name:       "removeAll failure",
			failRemove: true,
			wantErr:    true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			mu.Lock()
			defer mu.Unlock()

			if tc.failTempDir {
				orig := mkdirTempFn
				mkdirTempFn = func(dir, pattern string) (string, error) {
					return "", errors.New("temp dir failure")
				}
				t.Cleanup(func() { mkdirTempFn = orig })
			}
			if tc.failReadDir {
				orig := readDirFn
				readDirFn = func(name string) ([]os.DirEntry, error) {
					return nil, errors.New("readdir failure")
				}
				t.Cleanup(func() { readDirFn = orig })
			}
			if tc.failRemove {
				orig := removeAllFn
				removeAllFn = func(path string) error {
					return errors.New("remove failed")
				}
				t.Cleanup(func() { removeAllFn = orig })
			}

			var metaFn func(string) error
			if tc.metaWriteErr {
				metaFn = func(dir string) error {
					return errors.New("meta failed")
				}
			} else {
				metaFn = func(dir string) error {
					return os.WriteFile(filepath.Join(dir, "meta.cov"), []byte("meta data"), 0o600)
				}
			}

			var countersFn func(string) error
			if tc.countersErr {
				countersFn = func(dir string) error {
					return errors.New("counters failed")
				}
			} else {
				countersFn = func(dir string) error {
					return os.WriteFile(filepath.Join(dir, "counters.cov"), []byte("counter data"), 0o600)
				}
			}

			var w io.Writer = &bytes.Buffer{}
			if tc.failWriter {
				w = failWriter{err: errors.New("write failure")}
			}

			err := WriteArchive(w, metaFn, countersFn)
			if (err != nil) != tc.wantErr {
				t.Fatalf("WriteArchive() error = %v, wantErr %v", err, tc.wantErr)
			}
			if !tc.wantErr && tc.wantFiles > 0 {
				buf, ok := w.(*bytes.Buffer)
				if !ok {
					t.Fatalf("expected *bytes.Buffer for successful archive inspection")
				}
				tr := tar.NewReader(buf)
				count := 0
				for {
					_, err := tr.Next()
					if errors.Is(err, io.EOF) {
						break
					}
					if err != nil {
						t.Fatalf("reading tar archive: %v", err)
					}
					count++
				}
				if count != tc.wantFiles {
					t.Fatalf("got %d archive entries, want %d", count, tc.wantFiles)
				}
			}
		})
	}
}
