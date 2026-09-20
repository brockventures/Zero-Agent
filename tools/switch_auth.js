#!/usr/bin/env node
/**
 * Nintendo Switch OAuth helper for Zero.
 * Supports both Coral (NSO) and Moon (Parental Controls) decoupled PKCE authentication.
 */

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import { initStorage } from '/usr/local/lib/node_modules/nxapi/dist/util/storage.js';
import { getToken } from '/usr/local/lib/node_modules/nxapi/dist/common/auth/coral.js';
import { getPctlToken } from '/usr/local/lib/node_modules/nxapi/dist/common/auth/moon.js';
import { getNintendoAccountSessionToken } from '/usr/local/lib/node_modules/nxapi/dist/api/na.js';
import { ZNCA_CLIENT_ID } from '/usr/local/lib/node_modules/nxapi/dist/api/coral.js';
import { ZNMA_CLIENT_ID } from '/usr/local/lib/node_modules/nxapi/dist/api/moon.js';

const PENDING_FILE = '/workspace/data/nxapi_pending_auth.json';
const PENDING_MOON_FILE = '/workspace/data/nxapi_pending_moon_auth.json';
const TOKEN_FILE = '/workspace/data/nxapi_session_token.json';
const MOON_TOKEN_FILE = '/workspace/data/nxapi_moon_session_token.json';
const DATA_PATH = '/workspace/data/nxapi';

// --- Coral (NSO) ---

async function generateCoral() {
    const state = crypto.randomBytes(36).toString('base64url');
    const verifier = crypto.randomBytes(32).toString('base64url');
    const challenge = crypto.createHash('sha256').update(verifier).digest().toString('base64url');
    
    const params = {
        state,
        redirect_uri: 'npf71b963c1b7b6d119://auth',
        client_id: ZNCA_CLIENT_ID,
        scope: 'openid user user.birthday user.mii user.screenName',
        response_type: 'session_token_code',
        session_token_code_challenge: challenge,
        session_token_code_challenge_method: 'S256',
        theme: 'login_form',
    };
    
    const authUrl = 'https://accounts.nintendo.com/connect/1.0.0/authorize?' +
        new URLSearchParams(params).toString();
        
    const pendingData = {
        state,
        verifier,
        challenge,
        authUrl,
        created_at: new Date().toISOString()
    };
    
    fs.writeFileSync(PENDING_FILE, JSON.stringify(pendingData, null, 2), 'utf-8');
    
    console.log(JSON.stringify({
        status: 'ready',
        service: 'coral',
        authUrl,
        message: 'Saved pending Coral PKCE verifier'
    }));
}

async function redeemCoral(applink) {
    if (!fs.existsSync(PENDING_FILE)) {
        console.error('Error: No pending auth record found at ' + PENDING_FILE);
        process.exit(1);
    }
    
    const pendingData = JSON.parse(fs.readFileSync(PENDING_FILE, 'utf-8'));
    const verifier = pendingData.verifier;
    
    let linkStr = applink.trim();
    if (!linkStr.includes('#') && linkStr.includes('&')) {
        linkStr = 'npf71b963c1b7b6d119://auth#' + linkStr;
    }
    
    const hashIdx = linkStr.indexOf('#');
    const hashPart = hashIdx >= 0 ? linkStr.substring(hashIdx + 1) : linkStr;
    const authorisedparams = new URLSearchParams(hashPart);
    
    const code = authorisedparams.get('session_token_code');
    if (!code) {
        console.error('Error: Could not extract session_token_code from provided link');
        process.exit(1);
    }
    
    console.log('[Coral Auth] Exchanging session token code with Nintendo...');
    const token = await getNintendoAccountSessionToken(code, verifier, ZNCA_CLIENT_ID);
    console.log('[Coral Auth] Session token acquired successfully.');

    fs.writeFileSync(TOKEN_FILE, JSON.stringify({
        saved_at: new Date().toISOString(),
        session_token: token.session_token,
        code: token.code
    }, null, 2), 'utf-8');
    console.log('[Coral Auth] Persisted session token to ' + TOKEN_FILE);
    
    const storage = await initStorage(DATA_PATH);
    const { nso, data } = await getToken(storage, token.session_token);
    console.log(`[Coral Auth] Authenticated as ${data.user.screenName} (${data.nsoAccount.user.name})`);
    
    await storage.setItem('NintendoAccountToken.' + data.user.id, token.session_token);
    const users = new Set(await storage.getItem('NintendoAccountIds') ?? []);
    users.add(data.user.id);
    await storage.setItem('NintendoAccountIds', [...users]);
    await storage.setItem('SelectedUser', data.user.id);
    console.log('[Coral Auth] Stored credentials in ' + DATA_PATH);
    
    if (fs.existsSync(PENDING_FILE)) {
        fs.unlinkSync(PENDING_FILE);
    }
    
    return data;
}

async function resumeCoral() {
    if (!fs.existsSync(TOKEN_FILE)) {
        console.error('Error: No stored session token found at ' + TOKEN_FILE);
        process.exit(1);
    }
    const tokenData = JSON.parse(fs.readFileSync(TOKEN_FILE, 'utf-8'));
    console.log('[Coral Auth] Attempting to resume Coral session using stored token...');
    const storage = await initStorage(DATA_PATH);
    const { nso, data } = await getToken(storage, tokenData.session_token);
    console.log(`[Coral Auth] Authenticated as ${data.user.screenName} (${data.nsoAccount.user.name})`);
    
    await storage.setItem('NintendoAccountToken.' + data.user.id, tokenData.session_token);
    const users = new Set(await storage.getItem('NintendoAccountIds') ?? []);
    users.add(data.user.id);
    await storage.setItem('NintendoAccountIds', [...users]);
    await storage.setItem('SelectedUser', data.user.id);
    console.log('[Coral Auth] Stored credentials in ' + DATA_PATH);
    return data;
}

// --- Moon (Parental Controls) ---

async function generateMoon() {
    const state = crypto.randomBytes(36).toString('base64url');
    const verifier = crypto.randomBytes(32).toString('base64url');
    const challenge = crypto.createHash('sha256').update(verifier).digest().toString('base64url');
    
    const params = {
        state,
        redirect_uri: 'npf54789befb391a838://auth',
        client_id: ZNMA_CLIENT_ID,
        scope: [
            'openid',
            'user',
            'user.mii',
            'moonUser:administration',
            'moonDevice:create',
            'moonOwnedDevice:administration',
            'moonParentalControlSetting',
            'moonParentalControlSetting:update',
            'moonParentalControlSettingState',
            'moonPairingState',
            'moonSmartDevice:administration',
            'moonDailySummary',
            'moonMonthlySummary',
        ].join(' '),
        response_type: 'session_token_code',
        session_token_code_challenge: challenge,
        session_token_code_challenge_method: 'S256',
        theme: 'login_form',
    };
    
    const authUrl = 'https://accounts.nintendo.com/connect/1.0.0/authorize?' +
        new URLSearchParams(params).toString();
        
    const pendingData = {
        state,
        verifier,
        challenge,
        authUrl,
        created_at: new Date().toISOString()
    };
    
    fs.writeFileSync(PENDING_MOON_FILE, JSON.stringify(pendingData, null, 2), 'utf-8');
    
    console.log(JSON.stringify({
        status: 'ready',
        service: 'moon',
        authUrl,
        message: 'Saved pending Moon PKCE verifier'
    }));
}

async function redeemMoon(applink) {
    if (!fs.existsSync(PENDING_MOON_FILE)) {
        console.error('Error: No pending auth record found at ' + PENDING_MOON_FILE);
        process.exit(1);
    }
    
    const pendingData = JSON.parse(fs.readFileSync(PENDING_MOON_FILE, 'utf-8'));
    const verifier = pendingData.verifier;
    
    let linkStr = applink.trim();
    if (!linkStr.includes('#') && linkStr.includes('&')) {
        linkStr = 'npf54789befb391a838://auth#' + linkStr;
    }
    
    const hashIdx = linkStr.indexOf('#');
    const hashPart = hashIdx >= 0 ? linkStr.substring(hashIdx + 1) : linkStr;
    const authorisedparams = new URLSearchParams(hashPart);
    
    const code = authorisedparams.get('session_token_code');
    if (!code) {
        console.error('Error: Could not extract session_token_code from provided link');
        process.exit(1);
    }
    
    console.log('[Moon Auth] Exchanging session token code with Nintendo...');
    const token = await getNintendoAccountSessionToken(code, verifier, ZNMA_CLIENT_ID);
    console.log('[Moon Auth] Session token acquired successfully.');

    fs.writeFileSync(MOON_TOKEN_FILE, JSON.stringify({
        saved_at: new Date().toISOString(),
        session_token: token.session_token,
        code: token.code
    }, null, 2), 'utf-8');
    console.log('[Moon Auth] Persisted session token to ' + MOON_TOKEN_FILE);
    
    const storage = await initStorage(DATA_PATH);
    const { moon, data } = await getPctlToken(storage, token.session_token);
    console.log(`[Moon Auth] Authenticated as ${data.user.nickname} (${data.user.id})`);
    
    await storage.setItem('NintendoAccountToken-pctl.' + data.user.id, token.session_token);
    const users = new Set(await storage.getItem('NintendoAccountIds') ?? []);
    users.add(data.user.id);
    await storage.setItem('NintendoAccountIds', [...users]);
    await storage.setItem('SelectedUser', data.user.id);
    console.log('[Moon Auth] Stored credentials in ' + DATA_PATH);
    
    if (fs.existsSync(PENDING_MOON_FILE)) {
        fs.unlinkSync(PENDING_MOON_FILE);
    }
    
    return data;
}

async function resumeMoon() {
    if (!fs.existsSync(MOON_TOKEN_FILE)) {
        console.error('Error: No stored Moon session token found at ' + MOON_TOKEN_FILE);
        process.exit(1);
    }
    const tokenData = JSON.parse(fs.readFileSync(MOON_TOKEN_FILE, 'utf-8'));
    console.log('[Moon Auth] Attempting to resume Moon session using stored token...');
    const storage = await initStorage(DATA_PATH);
    const { moon, data } = await getPctlToken(storage, tokenData.session_token);
    console.log(`[Moon Auth] Authenticated as ${data.user.nickname} (${data.user.id})`);
    
    await storage.setItem('NintendoAccountToken-pctl.' + data.user.id, tokenData.session_token);
    const users = new Set(await storage.getItem('NintendoAccountIds') ?? []);
    users.add(data.user.id);
    await storage.setItem('NintendoAccountIds', [...users]);
    await storage.setItem('SelectedUser', data.user.id);
    console.log('[Moon Auth] Stored credentials in ' + DATA_PATH);
    return data;
}

// --- CLI Dispatcher ---

const cmd = process.argv[2];

if (cmd === 'generate' || cmd === 'generate-coral') {
    generateCoral().catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'generate-moon') {
    generateMoon().catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'redeem' || cmd === 'redeem-coral') {
    const link = process.argv[3];
    if (!link) { console.error('Usage: switch_auth.js redeem <link>'); process.exit(1); }
    redeemCoral(link).catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'redeem-moon') {
    const link = process.argv[3];
    if (!link) { console.error('Usage: switch_auth.js redeem-moon <link>'); process.exit(1); }
    redeemMoon(link).catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'resume' || cmd === 'resume-coral') {
    resumeCoral().catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'resume-moon') {
    resumeMoon().catch(err => { console.error(err); process.exit(1); });
} else if (cmd === 'status') {
    const res = {
        coral: fs.existsSync(TOKEN_FILE),
        moon: fs.existsSync(MOON_TOKEN_FILE)
    };
    console.log(JSON.stringify(res));
} else {
    console.error('Usage: switch_auth.js [generate|generate-moon|redeem <link>|redeem-moon <link>|resume|resume-moon|status]');
    process.exit(1);
}

