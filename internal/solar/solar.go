package solar

import (
	"fmt"
	"math"
	"time"
)

const (
	// OfficialZenith is the standard solar zenith for official sunrise and sunset
	// accounting for atmospheric refraction and solar disc semi-diameter (90° 50').
	OfficialZenith = 90.8333
	degToRad       = math.Pi / 180.0
	radToDeg       = 180.0 / math.Pi
)

// CalculateSunriseSunset computes the official sunrise and sunset times for a given
// latitude, longitude, and date in the date's local timezone using the NOAA solar algorithm.
func CalculateSunriseSunset(lat, lon float64, date time.Time) (time.Time, time.Time, error) {
	loc := date.Location()
	year, month, day := date.Date()
	localMidnight := time.Date(year, month, day, 0, 0, 0, 0, loc)

	sunriseUTC, err := calculateEvent(lat, lon, localMidnight, true)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("calculating sunrise: %w", err)
	}

	sunsetUTC, err := calculateEvent(lat, lon, localMidnight, false)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("calculating sunset: %w", err)
	}

	return sunriseUTC.In(loc), sunsetUTC.In(loc), nil
}

// IsNight returns true if the specified time is during the night (i.e. before sunrise
// or after sunset) for the given latitude and longitude.
func IsNight(lat, lon float64, t time.Time) bool {
	sunrise, sunset, err := CalculateSunriseSunset(lat, lon, t)
	if err != nil {
		// Fallback to simple hour heuristic if calculation fails:
		// Night is between 8:00 PM (20:00) and 6:30 AM
		h := t.Hour()
		return h < 6 || h >= 20
	}

	// If now is before today's sunrise or after today's sunset, it's night.
	return t.Before(sunrise) || t.After(sunset) || t.Equal(sunset)
}

func calculateEvent(lat, lon float64, date time.Time, isSunrise bool) (time.Time, error) {
	dayOfYear := date.YearDay()
	lngHour := lon / 15.0

	var tApprox float64
	if isSunrise {
		tApprox = float64(dayOfYear) + ((6.0 - lngHour) / 24.0)
	} else {
		tApprox = float64(dayOfYear) + ((18.0 - lngHour) / 24.0)
	}

	// 1. Sun's mean anomaly M
	M := (0.9856 * tApprox) - 3.289
	MRad := M * degToRad

	// 2. Sun's true longitude L
	L := M + (1.916 * math.Sin(MRad)) + (0.020 * math.Sin(2*MRad)) + 282.634
	L = normalizeDegrees(L)
	LRad := L * degToRad

	// 3. Right Ascension RA
	RA := math.Atan(0.91764*math.Tan(LRad)) * radToDeg
	RA = normalizeDegrees(RA)

	// Right ascension quadrant adjustment
	lQuadrant := math.Floor(L/90.0) * 90.0
	raQuadrant := math.Floor(RA/90.0) * 90.0
	RA = (RA + (lQuadrant - raQuadrant)) / 15.0 // Convert to hours

	// 4. Sun's declination
	sinDec := 0.39782 * math.Sin(LRad)
	cosDec := math.Cos(math.Asin(sinDec))

	// 5. Sun's local hour angle H
	zenithRad := OfficialZenith * degToRad
	latRad := lat * degToRad
	cosH := (math.Cos(zenithRad) - (sinDec * math.Sin(latRad))) / (cosDec * math.Cos(latRad))

	if cosH > 1.0 {
		return time.Time{}, fmt.Errorf("sun never rises on this date at coordinates (%.4f, %.4f)", lat, lon)
	}
	if cosH < -1.0 {
		return time.Time{}, fmt.Errorf("sun never sets on this date at coordinates (%.4f, %.4f)", lat, lon)
	}

	var H float64
	if isSunrise {
		H = 360.0 - (math.Acos(cosH) * radToDeg)
	} else {
		H = math.Acos(cosH) * radToDeg
	}
	H /= 15.0 // Convert to hours

	// 6. Local mean time of rising/setting
	T := H + RA - (0.06571 * tApprox) - 6.622

	// 7. Universal Time (UTC)
	UT := T - lngHour
	for UT < 0.0 {
		UT += 24.0
	}
	for UT >= 24.0 {
		UT -= 24.0
	}

	hours := int(UT)
	remMinutes := (UT - float64(hours)) * 60.0
	minutes := int(remMinutes)
	seconds := int((remMinutes - float64(minutes)) * 60.0)

	resultUTC := time.Date(date.Year(), date.Month(), date.Day(), hours, minutes, seconds, 0, time.UTC)
	// Ensure the resulting time falls on the requested local calendar day
	resultLocal := resultUTC.In(date.Location())
	targetYear, targetMonth, targetDay := date.Date()
	for resultLocal.Year() < targetYear || (resultLocal.Year() == targetYear && resultLocal.Month() < targetMonth) || (resultLocal.Year() == targetYear && resultLocal.Month() == targetMonth && resultLocal.Day() < targetDay) {
		resultUTC = resultUTC.Add(24 * time.Hour)
		resultLocal = resultUTC.In(date.Location())
	}
	for resultLocal.Year() > targetYear || (resultLocal.Year() == targetYear && resultLocal.Month() > targetMonth) || (resultLocal.Year() == targetYear && resultLocal.Month() == targetMonth && resultLocal.Day() > targetDay) {
		resultUTC = resultUTC.Add(-24 * time.Hour)
		resultLocal = resultUTC.In(date.Location())
	}
	return resultUTC, nil
}

func normalizeDegrees(deg float64) float64 {
	deg = math.Mod(deg, 360.0)
	if deg < 0 {
		deg += 360.0
	}
	return deg
}
