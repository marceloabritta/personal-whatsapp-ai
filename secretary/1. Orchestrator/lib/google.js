// ============================================================================
//  Google OAuth — the one place the refresh token becomes an authorized client.
//
//  calendar_action and task_action each built their own OAuth2 client from the same
//  three env vars. That's the only thing they actually share, so that's all this
//  exports: the AUTH. Each skill still constructs its own service, keeping the rails
//  ignorant of which Google APIs exist:
//
//    google.calendar({ version: "v3", auth: googleAuth(env) })
//    google.tasks({ version: "v1", auth: googleAuth(env) })
//
//  GOOGLE_REFRESH_TOKEN must carry the scope for EVERY service that uses it (adding
//  a scope means re-consenting and re-issuing the token — see the skills' SKILL.md).
//  Without the right scope the call 401s and the skill replies with its failure copy.
// ============================================================================
import { google } from "googleapis";

export function googleAuth(env) {
  const o = new google.auth.OAuth2(
    env.GOOGLE_CLIENT_ID,
    env.GOOGLE_CLIENT_SECRET
  );
  o.setCredentials({ refresh_token: env.GOOGLE_REFRESH_TOKEN });
  return o;
}
