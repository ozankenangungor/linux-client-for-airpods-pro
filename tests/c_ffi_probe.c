/* Repository-only consumer. Uses the checked-in public C header exclusively. */
#include "airpods_client.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

_Static_assert(sizeof(airpods_hr_sample_t) == 16, "sample ABI size");
_Static_assert(offsetof(airpods_hr_sample_t, bpm) == 0, "bpm offset");
_Static_assert(offsetof(airpods_hr_sample_t, source_side) == 4, "side offset");
_Static_assert(offsetof(airpods_hr_sample_t, source_side_raw) == 8, "raw offset");
_Static_assert(offsetof(airpods_hr_sample_t, reserved) == 12, "reserved offset");
_Static_assert(sizeof(airpods_string_view_t) == sizeof(void *) + sizeof(size_t), "view ABI size");
_Static_assert(offsetof(airpods_string_view_t, data) == 0, "view data offset");
_Static_assert(offsetof(airpods_string_view_t, len) == sizeof(void *), "view len offset");

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "C probe check failed at line %d\n", __LINE__); exit(1); \
} } while (0)
#define OK(call) do { CHECK((call) == AIRPODS_RESULT_OK); CHECK(error == NULL); } while (0)

static void typed_error(airpods_result_t result, airpods_error_t **error,
                        airpods_error_kind_t kind) {
    CHECK(result == AIRPODS_RESULT_ERROR);
    CHECK(*error != NULL);
    CHECK(airpods_error_kind(*error) == kind);
    airpods_string_view_t code = airpods_error_daemon_code(*error);
    CHECK(code.data == NULL && code.len == 0);
    airpods_error_free(*error);
    *error = NULL;
}

static void phase(const char *value) {
    CHECK(puts(value) >= 0);
    CHECK(fflush(stdout) == 0);
}

static void sample(airpods_client_t *client, uint32_t timeout,
                   uint32_t bpm, uint32_t side, uint32_t raw) {
    airpods_error_t *error = NULL;
    airpods_hr_sample_t value = {999u, 999u, 999u, 999u};
    OK(airpods_client_hr_next(client, timeout, &value, &error));
    CHECK(value.bpm == bpm && value.source_side == side);
    CHECK(value.source_side_raw == raw && value.reserved == 0);
    CHECK(printf("SAMPLE %u %u %u %u\n", (unsigned)value.bpm,
                 (unsigned)value.source_side, (unsigned)value.source_side_raw,
                 (unsigned)value.reserved) > 0);
    CHECK(fflush(stdout) == 0);
}

static void poll_timeout(airpods_client_t *client, uint32_t timeout) {
    airpods_error_t *error = NULL;
    airpods_hr_sample_t value = {999u, 999u, 999u, 999u};
    CHECK(airpods_client_hr_next(client, timeout, &value, &error) == AIRPODS_RESULT_TIMEOUT);
    CHECK(error == NULL);
    CHECK(value.bpm == 0 && value.source_side == 0 && value.source_side_raw == 0 && value.reserved == 0);
}

static int failure(const char *mode, const char *path) {
    for (unsigned i = 0; i < 32; ++i) {
        airpods_client_t *client = NULL;
        airpods_error_t *error = NULL;
        airpods_result_t result = strcmp(mode, "explicit-error") == 0
            ? airpods_client_connect_to(path, &client, &error)
            : airpods_client_connect(&client, &error);
        CHECK(result == AIRPODS_RESULT_ERROR && client == NULL && error != NULL);
        CHECK(airpods_error_kind(error) == (strcmp(mode, "xdg-error") == 0
            ? AIRPODS_ERROR_XDG_RUNTIME_DIR_MISSING : AIRPODS_ERROR_CONNECT));
        airpods_string_view_t message = airpods_error_message(error);
        if (i == 0) {
            CHECK(fwrite(message.data, 1, message.len, stdout) == message.len);
            CHECK(putchar('\n') != EOF);
        }
        airpods_error_free(error);
    }
    return 0;
}

int main(int argc, char **argv) {
    CHECK(argc == 3);
    CHECK(airpods_client_c_abi_version() == AIRPODS_CLIENT_C_ABI_VERSION);
    CHECK(airpods_client_protocol_version() == 1u);
    airpods_client_free(NULL);
    airpods_error_free(NULL);
    airpods_hello_free(NULL);
    airpods_status_free(NULL);
    if (strstr(argv[1], "error") != NULL) return failure(argv[1], argv[2]);

    airpods_client_t *client = NULL;
    airpods_error_t *error = NULL;
    OK(airpods_client_connect_to(argv[2], &client, &error));
    CHECK(client != NULL);
    airpods_hello_t *hello = NULL;
    OK(airpods_client_hello(client, &hello, &error));
    airpods_string_view_t service = airpods_hello_service(hello);
    CHECK(service.len == strlen("airpods-hubd"));
    CHECK(memcmp(service.data, "airpods-hubd", service.len) == 0);
    CHECK(airpods_hello_experimental(hello) == 1u);
    airpods_hello_free(hello);
    OK(airpods_client_ping(client, &error));
    airpods_status_t *status = NULL;
    OK(airpods_client_status(client, &status, &error));
    CHECK(airpods_status_state(status) == AIRPODS_DAEMON_READY);
    CHECK(airpods_status_subscriber_count(status) == 0);
    CHECK(airpods_status_unknown_state(status).data == NULL);
    airpods_status_free(status);
    airpods_hr_sample_t value = {999u, 999u, 999u, 999u};
    airpods_result_t result = airpods_client_hr_next(client, 0, &value, &error);
    typed_error(result, &error, AIRPODS_ERROR_INVALID_STATE);
    CHECK(value.bpm == 0 && value.reserved == 0);
    result = airpods_client_hr_unsubscribe(client, &error);
    typed_error(result, &error, AIRPODS_ERROR_INVALID_STATE);
    OK(airpods_client_hr_subscribe(client, &error));
    result = airpods_client_hr_subscribe(client, &error);
    typed_error(result, &error, AIRPODS_ERROR_SUBSCRIPTION_ACTIVE);

    if (strcmp(argv[1], "scenario") == 0) {
        poll_timeout(client, 10);
        poll_timeout(client, 10);
        phase("TIMEOUTS");
        sample(client, AIRPODS_WAIT_FOREVER, 169, AIRPODS_SOURCE_LEFT, 0);
        phase("FIRST");
        sample(client, 1000, 88, AIRPODS_SOURCE_RIGHT, 0);
        sample(client, AIRPODS_WAIT_FOREVER, 88, AIRPODS_SOURCE_RIGHT, 0);
        sample(client, 1000, 74, AIRPODS_SOURCE_UNKNOWN, 37);
        poll_timeout(client, 0);
        OK(airpods_client_hr_unsubscribe(client, &error));
    } else {
        phase("READY");
        CHECK(getchar() == '\n');
        if (strcmp(argv[1], "free-active") == 0) {
            airpods_client_free(client);
            phase("DONE");
            return 0;
        }
        CHECK(strcmp(argv[1], "close-active") == 0);
    }
    OK(airpods_client_close(client, &error));
    OK(airpods_client_close(client, &error));
    result = airpods_client_ping(client, &error);
    typed_error(result, &error, AIRPODS_ERROR_INVALID_STATE);
    hello = NULL;
    result = airpods_client_hello(client, &hello, &error);
    typed_error(result, &error, AIRPODS_ERROR_INVALID_STATE);
    CHECK(hello == NULL);
    status = NULL;
    result = airpods_client_status(client, &status, &error);
    typed_error(result, &error, AIRPODS_ERROR_INVALID_STATE);
    CHECK(status == NULL);
    airpods_client_free(client);
    phase("DONE");
    return 0;
}
