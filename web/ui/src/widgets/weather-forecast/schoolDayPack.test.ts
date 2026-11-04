import { describe, expect, it } from 'vitest';
import {
  DEFAULT_SCHOOL_DAY_THRESHOLDS,
  type SchoolDayThresholds,
  resolveCharacterPackSpriteUrl,
  resolveClothingConditionState,
  selectDailyVariantIndex,
} from './schoolDayPack';

describe('schoolDayPack utilities', () => {
  describe('resolveClothingConditionState', () => {
    it('returns morning_rain or rainy_boots when precipProbPct >= rainThresholdPct', () => {
      // morning slot
      expect(resolveClothingConditionState(60, 35, 'weather-partly-cloudy', 'morning')).toBe(
        'morning_rain',
      );
      expect(resolveClothingConditionState(70, 80, 'weather-sunny', 'morning')).toBe(
        'morning_rain',
      );

      // afternoon slot
      expect(resolveClothingConditionState(60, 35, 'weather-partly-cloudy', 'afternoon')).toBe(
        'rainy_boots',
      );
      expect(resolveClothingConditionState(80, 50, 'weather-sunny', 'afternoon')).toBe(
        'rainy_boots',
      );

      // evening slot
      expect(resolveClothingConditionState(55, 40, 'weather-partly-cloudy', 'evening')).toBe(
        'rainy_boots',
      );
    });

    it('returns rain states when iconToken contains rain, pouring, or lightning even with low precipProbPct', () => {
      expect(resolveClothingConditionState(65, 0, 'weather-rainy', 'morning')).toBe('morning_rain');
      expect(resolveClothingConditionState(65, 10, 'weather-pouring', 'morning')).toBe(
        'morning_rain',
      );
      expect(resolveClothingConditionState(65, 5, 'weather-lightning', 'morning')).toBe(
        'morning_rain',
      );
      expect(resolveClothingConditionState(65, 0, 'weather-lightning-rainy', 'morning')).toBe(
        'morning_rain',
      );

      expect(resolveClothingConditionState(65, 0, 'weather-rainy', 'afternoon')).toBe(
        'rainy_boots',
      );
      expect(resolveClothingConditionState(65, 0, 'weather-pouring', 'evening')).toBe('rainy_boots');
      expect(resolveClothingConditionState(65, 0, 'weather-lightning', 'afternoon')).toBe(
        'rainy_boots',
      );
    });

    it('returns cold_puffer when tempF <= coldMaxF for all slots', () => {
      expect(resolveClothingConditionState(49, 10, 'weather-sunny', 'morning')).toBe('cold_puffer');
      expect(resolveClothingConditionState(32, 0, 'weather-cloudy', 'afternoon')).toBe(
        'cold_puffer',
      );
      expect(resolveClothingConditionState(45, 20, 'weather-partly-cloudy', 'evening')).toBe(
        'cold_puffer',
      );
    });

    it('returns morning_mild for morning slot when tempF > coldMaxF and dry', () => {
      expect(resolveClothingConditionState(50, 0, 'weather-sunny', 'morning')).toBe('morning_mild');
      expect(resolveClothingConditionState(64, 10, 'weather-sunny', 'morning')).toBe(
        'morning_mild',
      );
      expect(resolveClothingConditionState(75, 20, 'weather-sunny', 'morning')).toBe(
        'morning_mild',
      );
      expect(resolveClothingConditionState(90, 0, 'weather-sunny', 'morning')).toBe('morning_mild');
    });

    it('returns cool_evening for afternoon/evening when coldMaxF < tempF <= coolMaxF', () => {
      expect(resolveClothingConditionState(50, 0, 'weather-sunny', 'afternoon')).toBe(
        'cool_evening',
      );
      expect(resolveClothingConditionState(64, 15, 'weather-sunny', 'afternoon')).toBe(
        'cool_evening',
      );
      expect(resolveClothingConditionState(58, 0, 'weather-partly-cloudy', 'evening')).toBe(
        'cool_evening',
      );
    });

    it('returns warm_pants for afternoon/evening when coolMaxF < tempF <= warmMaxF', () => {
      expect(resolveClothingConditionState(65, 0, 'weather-sunny', 'afternoon')).toBe('warm_pants');
      expect(resolveClothingConditionState(77, 20, 'weather-sunny', 'afternoon')).toBe(
        'warm_pants',
      );
      expect(resolveClothingConditionState(70, 0, 'weather-partly-cloudy', 'evening')).toBe(
        'warm_pants',
      );
    });

    it('returns hot_dress for afternoon/evening when tempF > warmMaxF', () => {
      expect(resolveClothingConditionState(78, 0, 'weather-sunny', 'afternoon')).toBe('hot_dress');
      expect(resolveClothingConditionState(95, 10, 'weather-sunny', 'afternoon')).toBe('hot_dress');
      expect(resolveClothingConditionState(82, 0, 'weather-clear-night', 'evening')).toBe(
        'hot_dress',
      );
    });

    it('respects custom SchoolDayThresholds', () => {
      const custom: SchoolDayThresholds = {
        rainThresholdPct: 20,
        coldMaxF: 40,
        coolMaxF: 55,
        warmMaxF: 70,
      };

      // Rain trigger at lower threshold
      expect(resolveClothingConditionState(60, 22, 'weather-cloudy', 'morning', custom)).toBe(
        'morning_rain',
      );

      // Cold puffer at custom 40F
      expect(resolveClothingConditionState(40, 10, 'weather-sunny', 'afternoon', custom)).toBe(
        'cold_puffer',
      );

      // Cool evening at 41-55F
      expect(resolveClothingConditionState(52, 0, 'weather-sunny', 'afternoon', custom)).toBe(
        'cool_evening',
      );

      // Warm pants at 56-70F
      expect(resolveClothingConditionState(68, 0, 'weather-sunny', 'afternoon', custom)).toBe(
        'warm_pants',
      );

      // Hot dress at > 70F
      expect(resolveClothingConditionState(72, 0, 'weather-sunny', 'afternoon', custom)).toBe(
        'hot_dress',
      );
    });

    it('uses DEFAULT_SCHOOL_DAY_THRESHOLDS when thresholds parameter is omitted', () => {
      expect(DEFAULT_SCHOOL_DAY_THRESHOLDS.rainThresholdPct).toBe(35);
      expect(DEFAULT_SCHOOL_DAY_THRESHOLDS.coldMaxF).toBe(49);
      expect(DEFAULT_SCHOOL_DAY_THRESHOLDS.coolMaxF).toBe(64);
      expect(DEFAULT_SCHOOL_DAY_THRESHOLDS.warmMaxF).toBe(77);

      expect(resolveClothingConditionState(49, 0, 'weather-sunny', 'morning')).toBe('cold_puffer');
      expect(resolveClothingConditionState(50, 0, 'weather-sunny', 'morning')).toBe('morning_mild');
      expect(resolveClothingConditionState(64, 0, 'weather-sunny', 'afternoon')).toBe(
        'cool_evening',
      );
      expect(resolveClothingConditionState(77, 0, 'weather-sunny', 'afternoon')).toBe('warm_pants');
      expect(resolveClothingConditionState(78, 0, 'weather-sunny', 'afternoon')).toBe('hot_dress');
    });
  });

  describe('selectDailyVariantIndex', () => {
    it('returns deterministic variant indices within [1, variantsCount]', () => {
      const v1 = selectDailyVariantIndex('2026-10-31', 'morning', 5);
      const v2 = selectDailyVariantIndex('2026-10-31', 'morning', 5);
      expect(v1).toBe(v2);
      expect(v1).toBeGreaterThanOrEqual(1);
      expect(v1).toBeLessThanOrEqual(5);

      const vAfternoon = selectDailyVariantIndex('2026-10-31', 'afternoon', 5);
      expect(vAfternoon).toBeGreaterThanOrEqual(1);
      expect(vAfternoon).toBeLessThanOrEqual(5);

      const vEvening = selectDailyVariantIndex('2026-10-31', 'evening', 5);
      expect(vEvening).toBeGreaterThanOrEqual(1);
      expect(vEvening).toBeLessThanOrEqual(5);
    });

    it('respects custom variantsCount and handles non-positive counts safely', () => {
      for (let i = 0; i < 10; i++) {
        const v = selectDailyVariantIndex(`2026-11-0${i}`, 'morning', 3);
        expect(v).toBeGreaterThanOrEqual(1);
        expect(v).toBeLessThanOrEqual(3);
      }

      // Edge case: variantsCount <= 0 clamps to count 1
      const clamped = selectDailyVariantIndex('2026-10-31', 'morning', 0);
      expect(clamped).toBe(1);
      const clampedNeg = selectDailyVariantIndex('2026-10-31', 'morning', -5);
      expect(clampedNeg).toBe(1);
    });
  });

  describe('resolveCharacterPackSpriteUrl', () => {
    it('formats sprite URL with default assetsBase', () => {
      const url = resolveCharacterPackSpriteUrl('ghibli_school_day', 'cold_puffer', 3);
      expect(url).toBe('/static/ui/assets/weather-packs/ghibli_school_day/cold_puffer_v3.jpg');
    });

    it('formats sprite URL with custom assetsBase', () => {
      const url = resolveCharacterPackSpriteUrl(
        'custom_pack',
        'morning_rain',
        1,
        '/custom/assets/packs',
      );
      expect(url).toBe('/custom/assets/packs/custom_pack/morning_rain_v1.jpg');
    });
  });
});

