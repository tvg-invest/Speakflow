/*
 * SpeakFlow launcher — exec to the venv Python interpreter.
 */
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>

extern char **environ;

int main() {
    char *home = getenv("HOME");
    if (!home) {
        fprintf(stderr, "HOME not set\n");
        return 1;
    }
    char python[512];
    char script[512];
    char dir[512];
    snprintf(python, sizeof(python), "%s/.speakflow/venv/bin/python3", home);
    snprintf(script, sizeof(script), "%s/.speakflow/run.py", home);
    snprintf(dir, sizeof(dir), "%s/.speakflow", home);
    chdir(dir);
    char *argv[] = {"SpeakFlow", script, NULL};
    execve(python, argv, environ);
    perror("execve failed");
    return 1;
}
