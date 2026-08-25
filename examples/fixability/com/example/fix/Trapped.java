package com.example.fix;

public class Trapped {
    private String who() { return "PARENT"; }
    public String call() { return who(); }
}
